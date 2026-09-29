"""ai-auditor 应用配置。

所有配置可通过环境变量覆盖，前缀 AUDITOR_，如 AUDITOR_DATABASE_URL。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUDITOR_", env_file=".env", extra="ignore")

    app_name: str = "ai-auditor"
    # 生产为 PostgreSQL；测试用 sqlite（JSON 列两端兼容）
    database_url: str = "postgresql+psycopg://auditor:auditor@localhost:5432/auditor"
    redis_url: str = "redis://localhost:6379/0"

    # webhook 鉴权：HMAC-SHA256 + 时间戳防重放
    webhook_secret_default: str = "dev-secret"
    webhook_replay_window_seconds: int = 300

    # 队列派发：inline=进程内后台执行（开发/测试）；arq=Redis 队列（生产，失败自动降级 inline）
    queue_mode: str = "inline"  # inline | arq
    arq_queue_name: str = "auditor"

    # LLM 模型网关（NewAPI，OpenAI 兼容协议）
    llm_enabled: bool = False  # P0 默认关闭；P1 审核链就绪后置 True
    llm_base_url: str = "http://10.26.8.43:3002/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0
    llm_retry_times: int = 2
    llm_injection_scan: bool = True  # materials 内容注入模式扫描（详细设计 §9.2）

    # 决策矩阵阈值（可按 flow 覆盖，此处为全局默认）
    conf_high: float = 0.90
    conf_low: float = 0.60

    # RAG 制度知识库（P1.3）
    rag_enabled: bool = False
    rag_top_k: int = 5
    rag_rrf_k: int = 60  # RRF 融合常数
    embedding_model: str = "text-embedding-3-small"
    rag_max_excerpt_chars: int = 400

    # 回写执行（P1.4）：默认 mock 适配器；生产配置 writeback_config 里的 http_endpoint
    writeback_http_timeout: float = 10.0


settings = Settings()
