#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio

from typing import AsyncGenerator

from pipecat.processors.frame_processor import FrameDirection
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    StartInterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.ai_services import TTSService

from loguru import logger

# See .env.example for LMNT configuration needed
try:
    from lmnt.api import Speech
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error(
        "In order to use LMNT, you need to `pip install pipecat-ai[lmnt]`. Also, set `LMNT_API_KEY` environment variable."
    )
    raise Exception(f"Missing module: {e}")


class LmntTTSService(TTSService):
    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        sample_rate: int = 24000,
        language: str = "en",
        **kwargs,
    ):
        # Let TTSService produce TTSStoppedFrames after a short delay of
        # no activity.
        super().__init__(push_stop_frames=True, sample_rate=sample_rate, **kwargs)

        self._api_key = api_key
        self._voice_id = voice_id
        self._output_format = {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": sample_rate,
        }
        self._language = language

        self._speech = None
        self._connection = None
        self._receive_task = None
        # Indicates if we have sent TTSStartedFrame. It will reset to False when
        # there's an interruption or TTSStoppedFrame.
        self._started = False

    def can_generate_metrics(self) -> bool:
        return True

    async def set_voice(self, voice: str):
        logger.debug(f"Switching TTS voice to: [{voice}]")
        self._voice_id = voice

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        await super().push_frame(frame, direction)
        if isinstance(frame, (TTSStoppedFrame, StartInterruptionFrame)):
            self._started = False

    async def _connect(self):
        try:
            self._speech = Speech()
            self._connection = await self._speech.synthesize_streaming(
                self._voice_id, format="raw", sample_rate=self._output_format["sample_rate"]
            )
            self._receive_task = self.get_event_loop().create_task(self._receive_task_handler())
        except Exception as e:
            logger.exception(f"{self} initialization error: {e}")
            self._connection = None

    async def _disconnect(self):
        try:
            await self.stop_all_metrics()

            if self._receive_task:
                self._receive_task.cancel()
                await self._receive_task
                self._receive_task = None
            if self._connection:
                await self._connection.socket.close()
                self._connection = None
            if self._speech:
                await self._speech.close()
                self._speech = None
            self._started = False
        except Exception as e:
            logger.exception(f"{self} error closing websocket: {e}")

    async def _receive_task_handler(self):
        try:
            async for msg in self._connection:
                if "error" in msg:
                    logger.error(f'{self} error: {msg["error"]}')
                    await self.push_frame(TTSStoppedFrame())
                    await self.stop_all_metrics()
                    await self.push_error(ErrorFrame(f'{self} error: {msg["error"]}'))
                elif "audio" in msg:
                    await self.stop_ttfb_metrics()
                    frame = TTSAudioRawFrame(
                        audio=msg["audio"],
                        sample_rate=self._output_format["sample_rate"],
                        num_channels=1,
                    )
                    await self.push_frame(frame)
                else:
                    logger.error(f"LMNT error, unknown message type: {msg}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"{self} exception: {e}")

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"Generating TTS: [{text}]")

        try:
            if not self._connection:
                await self._connect()

            if not self._started:
                await self.push_frame(TTSStartedFrame())
                await self.start_ttfb_metrics()
                self._started = True

            try:
                await self._connection.append_text(text)
                await self._connection.flush()
                await self.start_tts_usage_metrics(text)
            except Exception as e:
                logger.error(f"{self} error sending message: {e}")
                await self.push_frame(TTSStoppedFrame())
                await self._disconnect()
                await self._connect()
                return
            yield None
        except Exception as e:
            logger.exception(f"{self} exception: {e}")
