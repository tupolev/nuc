import os


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
FILES_DIR = os.getenv("FILES_DIR", "/data/files")
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "/data/auth.db")
WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "/workspace")

CHAT_CONCURRENCY = int(os.getenv("CHAT_CONCURRENCY", "2"))
EMBED_CONCURRENCY = int(os.getenv("EMBED_CONCURRENCY", "1"))
QUEUE_TIMEOUT = int(os.getenv("QUEUE_TIMEOUT", "30"))
TOOL_MAX_ITERATIONS = int(os.getenv("TOOL_MAX_ITERATIONS", "8"))
TOOL_ARG_MAX_LEN = int(os.getenv("TOOL_ARG_MAX_LEN", "50000"))
TOOL_OUTPUT_MAX_LEN = int(os.getenv("TOOL_OUTPUT_MAX_LEN", "100000"))
FILE_READ_MAX_BYTES = int(os.getenv("FILE_READ_MAX_BYTES", "200000"))
FILE_WRITE_MAX_BYTES = int(os.getenv("FILE_WRITE_MAX_BYTES", "200000"))
PATCH_MAX_BYTES = int(os.getenv("PATCH_MAX_BYTES", "200000"))
COMMAND_OUTPUT_MAX_BYTES = int(os.getenv("COMMAND_OUTPUT_MAX_BYTES", "100000"))
PYTHON_TIMEOUT = int(os.getenv("PYTHON_TIMEOUT", "30"))
PYTHON_CODE_MAX_LEN = int(os.getenv("PYTHON_CODE_MAX_LEN", "12000"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
HTTP_MAX_BYTES = int(os.getenv("HTTP_MAX_BYTES", "300000"))
SQLITE_QUERY_MAX_ROWS = int(os.getenv("SQLITE_QUERY_MAX_ROWS", "200"))
SHELL_TIMEOUT = int(os.getenv("SHELL_TIMEOUT", "20"))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen2.5-coder:7b")
TOOL_EXECUTION_MODE = os.getenv("TOOL_EXECUTION_MODE", "server").strip().lower()
AUTO_ENABLE_LOCAL_TOOLS = os.getenv("AUTO_ENABLE_LOCAL_TOOLS", "false").strip().lower() == "true"
SAFE_SHELL_COMMANDS = {
    item.strip()
    for item in os.getenv(
        "SAFE_SHELL_COMMANDS",
        "date,uname,whoami,uptime,df,free,ls,pwd,cat,head,tail,sed,rg,find,stat,du",
    ).split(",")
    if item.strip()
}
EXEC_COMMAND_ALLOWLIST = {
    item.strip()
    for item in os.getenv(
        "EXEC_COMMAND_ALLOWLIST",
        "bash,sh,python3,pip,pip3,git,node,npm,pnpm,pytest,php,composer,phar,phpunit,phpcs,phpcbf,phpstan,php-cs-fixer,artisan,bin/console,apache2ctl,nginx",
    ).split(",")
    if item.strip()
}

PRIORITY_MAP = {"high": 0, "medium": 1, "low": 2}
