"""Core tests must never use real API keys."""
import os


os.environ["DEEPSEEK_KEY"] = "test-deepseek-key"
os.environ["AGNES_KEY"] = "test-agnes-key"
os.environ["GPT_IMAGE_KEY"] = "test-gpt-image-key"
