import os
from pathlib import Path
import platform


class HookInstaller:
    def __init__(self):
        self.shell_configs = {
            "zsh": Path.home() / ".zshrc",
            "bash": Path.home() / ".bashrc",
            "fish": Path.home() / ".config" / "fish" / "config.fish",
            "powershell": self._get_powershell_profile(),
        }

        self.hook_marker = "# >>> Feishu-Mem 自动采集钩子 >>>"
        self.hook_marker_end = "# <<< Feishu-Mem 自动采集钩子 <<<"
        self.ps_hook_marker = "# >>> Feishu-Mem PowerShell Hook >>>"
        self.ps_hook_marker_end = "# <<< Feishu-Mem PowerShell Hook <<<"

    @staticmethod
    def _get_powershell_profile() -> Path:
        """获取PowerShell配置文件路径"""
        if platform.system() == "Windows":
            docs = Path(os.environ.get("USERPROFILE", Path.home())) / "Documents"
            return docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
        return Path.home() / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1"

    def _get_shell_type(self) -> str:
        """获取当前用户使用的Shell类型"""
        if platform.system() == "Windows":
            return "powershell"

        shell = os.getenv("SHELL", "")
        if "zsh" in shell:
            return "zsh"
        elif "bash" in shell:
            return "bash"
        elif "fish" in shell:
            return "fish"
        return "zsh"

    def _generate_hook_content(self, shell_type: str) -> str:
        """生成对应Shell的钩子内容"""
        mem_path = "mem"

        if shell_type in ("zsh", "bash"):
            return f"""
# Feishu-Mem 命令自动采集钩子
preexec() {{
    export FEISHU_MEM_START_TIME=$(date +%s)
}}

precmd() {{
    local exit_code=$?
    local command=$(fc -ln -1)
    if [ -n "$command" ] && [ -n "$FEISHU_MEM_START_TIME" ]; then
        local end_time=$(date +%s)
        {mem_path} hook "$command" --exit-code $exit_code --start-time $FEISHU_MEM_START_TIME --end-time $end_time >/dev/null 2>&1 &
        unset FEISHU_MEM_START_TIME
    fi
}}

# 命令补全支持
complete -C "{mem_path} completion" mem
"""
        elif shell_type == "fish":
            return f"""
# Feishu-Mem 命令自动采集钩子
function feishu_mem_preexec --on-event fish_preexec
    set -gx FEISHU_MEM_START_TIME (date +%s)
end

function feishu_mem_postexec --on-event fish_postexec
    set exit_code $status
    set command $argv[1]
    if [ -n "$command" ] && [ -n "$FEISHU_MEM_START_TIME" ]
        set end_time (date +%s)
        {mem_path} hook "$command" --exit-code $exit_code --start-time $FEISHU_MEM_START_TIME --end-time $end_time >/dev/null 2>&1 &
        set -e FEISHU_MEM_START_TIME
    end
end

# 命令补全支持
complete -c mem -a "({mem_path} completion)"
"""
        elif shell_type == "powershell":
            return f"""
# Feishu-Mem PowerShell 命令自动采集钩子
$FeishuMemStartTime = $null

$FeishuMemPreExec = {{
    $global:FeishuMemStartTime = [int][double]::Parse((Get-Date -UFormat %s))
}}

$FeishuMemPostExec = {{
    $exitCode = $LASTEXITCODE
    $command = (Get-History -Count 1).CommandLine
    if ($command -and $global:FeishuMemStartTime) {{
        $endTime = [int][double]::Parse((Get-Date -UFormat %s))
        Start-Process -NoNewWindow -FilePath "{mem_path}" -ArgumentList "hook", "`"$command`"", "--exit-code", $exitCode, "--start-time", $global:FeishuMemStartTime, "--end-time", $endTime -RedirectStandardOutput "NUL" -RedirectStandardError "NUL"
        $global:FeishuMemStartTime = $null
    }}
}}

# 注册Prompt函数以捕获命令执行
$function:OriginalPrompt = $function:prompt
function prompt {{
    $exitCode = $LASTEXITCODE
    & $FeishuMemPostExec
    $global:FeishuMemStartTime = [int][double]::Parse((Get-Date -UFormat %s))
    & $function:OriginalPrompt
}}

# 命令补全支持
Register-ArgumentCompleter -Native -CommandName mem -ScriptBlock {{
    param($commandName, $parameterName, $wordToComplete, $commandAst, $fakeBoundParameters)
    & "{mem_path}" completion $wordToComplete | ForEach-Object {{ [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }}
}}
"""
        return ""

    def install(self) -> None:
        """安装Shell钩子"""
        shell_type = self._get_shell_type()
        config_path = self.shell_configs[shell_type]

        if shell_type == "powershell":
            self._install_powershell_hook(config_path)
        else:
            self._install_unix_hook(shell_type, config_path)

    def _install_unix_hook(self, shell_type: str, config_path: Path) -> None:
        """安装Unix Shell钩子"""
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = ""

        if self.hook_marker in content:
            start = content.find(self.hook_marker)
            end = content.find(self.hook_marker_end) + len(self.hook_marker_end)
            if end > start:
                content = content[:start] + content[end:]

        hook_content = self._generate_hook_content(shell_type)
        new_content = f"{content.rstrip()}\n\n{self.hook_marker}\n{hook_content}\n{self.hook_marker_end}\n"

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    def _install_powershell_hook(self, config_path: Path) -> None:
        """安装PowerShell钩子"""
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = ""

        if self.ps_hook_marker in content:
            start = content.find(self.ps_hook_marker)
            end = content.find(self.ps_hook_marker_end) + len(self.ps_hook_marker_end)
            if end > start:
                content = content[:start] + content[end:]

        hook_content = self._generate_hook_content("powershell")
        new_content = f"{content.rstrip()}\n\n{self.ps_hook_marker}\n{hook_content}\n{self.ps_hook_marker_end}\n"

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)

    def uninstall(self) -> None:
        """卸载Shell钩子"""
        for shell_type, config_path in self.shell_configs.items():
            if not config_path.exists():
                continue

            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()

            marker = self.ps_hook_marker if shell_type == "powershell" else self.hook_marker
            marker_end = self.ps_hook_marker_end if shell_type == "powershell" else self.hook_marker_end

            if marker in content:
                start = content.find(marker)
                end = content.find(marker_end) + len(marker_end)
                if end > start:
                    content = content[:start] + content[end:]
                    with open(config_path, "w", encoding="utf-8") as f:
                        f.write(content)
