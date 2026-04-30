import os
from pathlib import Path
import platform

class HookInstaller:
    def __init__(self):
        self.shell_configs = {
            "zsh": Path.home() / ".zshrc",
            "bash": Path.home() / ".bashrc",
            "fish": Path.home() / ".config/fish/config.fish"
        }
        
        self.hook_marker = "# >>> Feishu-Mem 自动采集钩子 >>>"
        self.hook_marker_end = "# <<< Feishu-Mem 自动采集钩子 <<<"
    
    def _get_shell_type(self) -> str:
        """获取当前用户使用的Shell类型"""
        shell = os.getenv("SHELL", "")
        if "zsh" in shell:
            return "zsh"
        elif "bash" in shell:
            return "bash"
        elif "fish" in shell:
            return "fish"
        return "zsh"  # 默认zsh
    
    def _generate_hook_content(self, shell_type: str) -> str:
        """生成对应Shell的钩子内容"""
        mem_path = "mem"  # 假设mem命令已在PATH中
        
        if shell_type == "zsh" or shell_type == "bash":
            return f"""
# Feishu-Mem 命令自动采集钩子
preexec() {{
    # 记录命令执行开始时间（秒级时间戳，避免依赖外部计算工具）
    export FEISHU_MEM_START_TIME=$(date +%s)
}}

precmd() {{
    local exit_code=$?
    local command=$(fc -ln -1)
    if [ -n "$command" ] && [ -n "$FEISHU_MEM_START_TIME" ]; then
        local end_time=$(date +%s)
        # 直接传递时间戳到Python端计算，避免依赖bc/awk等外部命令
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
        # 直接传递时间戳到Python端计算，避免依赖bc/awk等外部命令
        {mem_path} hook "$command" --exit-code $exit_code --start-time $FEISHU_MEM_START_TIME --end-time $end_time >/dev/null 2>&1 &
        set -e FEISHU_MEM_START_TIME
    end
end

# 命令补全支持
complete -c mem -a "({mem_path} completion)"
"""
        return ""
    
    def install(self) -> None:
        """安装Shell钩子"""
        shell_type = self._get_shell_type()
        config_path = self.shell_configs[shell_type]
        
        # 读取现有配置
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = ""
        
        # 检查是否已经安装过钩子
        if self.hook_marker in content:
            # 移除旧的钩子内容
            start = content.find(self.hook_marker)
            end = content.find(self.hook_marker_end) + len(self.hook_marker_end)
            if end > start:
                content = content[:start] + content[end:]
        
        # 生成新的钩子内容
        hook_content = self._generate_hook_content(shell_type)
        new_content = f"{content.rstrip()}\n\n{self.hook_marker}\n{hook_content}\n{self.hook_marker_end}\n"
        
        # 写入配置文件
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    
    def uninstall(self) -> None:
        """卸载Shell钩子"""
        for shell_type, config_path in self.shell_configs.items():
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                if self.hook_marker in content:
                    start = content.find(self.hook_marker)
                    end = content.find(self.hook_marker_end) + len(self.hook_marker_end)
                    if end > start:
                        content = content[:start] + content[end:]
                        with open(config_path, "w", encoding="utf-8") as f:
                            f.write(content)
