from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    openai_base_url: str = "http://localhost:8000/v1"
    openai_api_key: str = "sk-placeholder"
    model_name: str = "openai/glm-4.7"
    proxy_port: int = 8082
    log_level: str = "INFO"


settings = Settings()
