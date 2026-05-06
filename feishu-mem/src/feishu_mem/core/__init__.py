"""Core modules for Feishu-Mem with side-effect-free lazy exports."""

__all__ = [
    "Storage",
    "CommandRecord",
    "CommandCollector",
    "ParsedCommand",
    "CommandContext",
    "CompletionEngine",
    "CompletionItem",
]


def __getattr__(name):
    if name in {"Storage", "CommandRecord"}:
        from .storage import CommandRecord, Storage

        return {"Storage": Storage, "CommandRecord": CommandRecord}[name]

    if name in {"CommandCollector", "ParsedCommand", "CommandContext"}:
        from .collector import CommandCollector, CommandContext, ParsedCommand

        return {
            "CommandCollector": CommandCollector,
            "ParsedCommand": ParsedCommand,
            "CommandContext": CommandContext,
        }[name]

    if name in {"CompletionEngine", "CompletionItem"}:
        from .completion import CompletionEngine, CompletionItem

        return {"CompletionEngine": CompletionEngine, "CompletionItem": CompletionItem}[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
