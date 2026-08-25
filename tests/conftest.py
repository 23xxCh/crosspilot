"""Core tests must never use real API keys."""
import os


os.environ["DEEPSEEK_KEY"] = "test-deepseek-key"
