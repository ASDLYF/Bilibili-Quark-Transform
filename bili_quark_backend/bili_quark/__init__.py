"""bili_quark：把 deepseek_bilibili 下已实测跑通的流水线封装成可参数化的后端 CLI。

入口：

    python -m bili_quark.cli <command> [options]

子命令：fetch / set-dir / resolve-dir / status / run / verify / list

设计原则：**改造而非重写**。原有 run_all.py / fetch_list.py / final_check.py
保持原样可用；已实测修复的坑（大小写不敏感响应头、单分片 PUT、_total 不可信、
上传重试重新取签名、递进式校验重试、Windows 删文件重试、线程加锁等）全部保留在
原有模块里，本包只做参数化与编排。
"""
import os
import sys

# 保证 `import bili_api` / `bili_dl` / `quark_upload` / `http_util` 一定能解析到
# 项目根目录（deepseek_bilibili），无论 --workdir 指向哪里。
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_HERE)
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

__all__ = ['PROJECT_DIR']
PROJECT_DIR = _PROJECT_DIR
