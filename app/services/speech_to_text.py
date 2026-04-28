import abc
from openai import OpenAI
import os
import config

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class SpeechToTextEngine(abc.ABC):
    @abc.abstractmethod
    def transcribe(self, audio_path):
        pass

class WhisperEngine(SpeechToTextEngine):
    def transcribe(self, audio_path):
        api_key=config.DevelopmentConfig.OPEN_AI  # Fetch API key from DevelopmentConfig
        client = OpenAI(api_key=api_key)
        with open(audio_path, "rb") as audio_file:
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        return response

class SpeechToTextEngineFactory:
    @staticmethod
    def create_engine(engine_type):
        if engine_type == "whisper":
            return WhisperEngine()
        # Add more engine types here
        else:
            raise ValueError(f"Unknown engine type: {engine_type}")

if __name__ == "__main__":
    # # Usage
    file="C:\\Users\\semis\\IdeaProjects\\EmiAi\\app\\static\\temp_audio\\tmpy56975dr.mp3"
    engine = SpeechToTextEngineFactory.create_engine("whisper")
    response = engine.transcribe(file)
    print(response.text)
