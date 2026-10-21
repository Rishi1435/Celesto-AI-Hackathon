import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    provider: str = "cloud"
    celesto_key: str = ""
    backend: str = "firecracker"
    template: str = "coding-agent"
    local_image: str = ""
    llm_base_url: str = "https://example-llm-host/v1"
    llm_key: str = ""
    llm_model: str = ""
    llm_free_only: bool = False
    database_url: str = "postgresql://ledger:ledger@ledger:5432/ledger"
    api_port: int = 8080
    max_concurrent: int = 1
    command_timeout: int = 60
    max_model_turns: int = 5
    sandbox_hourly_usd: float = 0.015
    llm_input_price: float = 0
    llm_output_price: float = 0
    access_token: str = ""

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / "config" / "runtime.env", override=False)
        return cls(
            provider=os.getenv("CELESTO_PROVIDER", "cloud"),
            celesto_key=os.getenv("CELESTO_API_KEY", ""),
            backend=os.getenv("CELESTO_BACKEND", "firecracker"),
            template=os.getenv("CELESTO_TEMPLATE", "coding-agent"),
            local_image=os.getenv("CELESTO_LOCAL_IMAGE", ""),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://example-llm-host/v1"),
            llm_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", ""),
            llm_free_only=os.getenv("LLM_FREE_ONLY", "false").lower() == "true",
            database_url=os.getenv("DATABASE_URL", "postgresql://ledger:ledger@ledger:5432/ledger"),
            api_port=int(os.getenv("API_PORT", "8080")),
            max_concurrent=int(os.getenv("MAX_CONCURRENT_JOBS", "1")),
            command_timeout=int(os.getenv("COMMAND_TIMEOUT_S", "60")),
            max_model_turns=int(os.getenv("MAX_MODEL_TURNS", "5")),
            sandbox_hourly_usd=float(os.getenv("SANDBOX_HOURLY_USD", "0.015")),
            llm_input_price=float(os.getenv("LLM_INPUT_USD_PER_MILLION", "0")),
            llm_output_price=float(os.getenv("LLM_OUTPUT_USD_PER_MILLION", "0")),
            access_token=os.getenv("API_ACCESS_TOKEN", ""),
        )

    def validate_agent(self):
        if self.provider not in {"local", "cloud"}:
            raise ValueError("CELESTO_PROVIDER must be local or cloud")
        for key, value in [("LLM_API_KEY", self.llm_key), ("LLM_MODEL", self.llm_model)]:
            if not value or value.startswith("replace-"):
                raise ValueError(f"Configure {key} in config/runtime.env")
        if self.provider == "cloud" and (not self.celesto_key or self.celesto_key == "replace-me"):
            raise ValueError("Configure CELESTO_API_KEY in config/runtime.env")
        if (
            not self.llm_base_url.startswith(("http://", "https://"))
            or "example-llm-host" in self.llm_base_url
        ):
            raise ValueError("Configure LLM_BASE_URL with an OpenAI-compatible endpoint")
        if not 1 <= self.max_concurrent <= 10 or not 1 <= self.max_model_turns <= 10:
            raise ValueError("Concurrency must be 1..10 and model turns 1..10")
        if not 1 <= self.command_timeout <= 300:
            raise ValueError("COMMAND_TIMEOUT_S must be 1..300")
        prices = [self.sandbox_hourly_usd, self.llm_input_price, self.llm_output_price]
        if any(not math.isfinite(price) or price < 0 for price in prices):
            raise ValueError("Prices must be finite and nonnegative")
        self.validate_model_policy()

    def validate_model_policy(self):
        if not self.llm_free_only:
            return
        if self.llm_base_url.rstrip("/") != "https://openrouter.ai/api/v1":
            raise ValueError("LLM_FREE_ONLY requires the official OpenRouter HTTPS endpoint")
        if not self.llm_model.endswith(":free"):
            raise ValueError("LLM_FREE_ONLY requires an explicit :free model; paid fallback is disabled")
        if self.llm_input_price != 0 or self.llm_output_price != 0:
            raise ValueError("LLM_FREE_ONLY requires zero model token prices")
