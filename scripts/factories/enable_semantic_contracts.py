#!/usr/bin/env python3
"""
启用语义契约配置

设置环境变量以启用语义契约生成和存储
"""
import os
import sys
from pathlib import Path

def main():
    print("="*80)
    print("启用语义契约配置")
    print("="*80)

    # 需要设置的环境变量
    env_vars = {
        'STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED': '1',
        'STRATEGY_FACTORY_PREDICTION_CONTRACT_ENABLED': '1',
        'STRATEGY_FACTORY_CONFIDENCE_CONTRACT_ENABLED': '1',
    }

    print("\n建议设置以下环境变量:")
    print("-" * 80)

    # Windows
    print("\nWindows (PowerShell):")
    for key, value in env_vars.items():
        print(f'  $env:{key}="{value}"')

    print("\nWindows (CMD):")
    for key, value in env_vars.items():
        print(f'  set {key}={value}')

    # Linux/Mac
    print("\nLinux/Mac (Bash):")
    for key, value in env_vars.items():
        print(f'  export {key}={value}')

    # 持久化配置
    print("\n" + "-" * 80)
    print("持久化配置（可选）")
    print("-" * 80)

    print("\n方法 1: 添加到 .env 文件")
    print("  创建或编辑: .env")
    for key, value in env_vars.items():
        print(f"  {key}={value}")

    print("\n方法 2: 添加到系统环境变量")
    print("  Windows: 系统属性 → 环境变量")
    print("  Linux/Mac: ~/.bashrc 或 ~/.zshrc")

    # 当前会话设置（仅示例）
    print("\n" + "="*80)
    print("当前 Python 会话设置（仅本次运行有效）")
    print("="*80)

    for key, value in env_vars.items():
        os.environ[key] = value
        print(f"  [OK] {key} = {value}")

    # 验证
    print("\n验证当前设置:")
    for key in env_vars:
        value = os.environ.get(key, '未设置')
        print(f"  {key}: {value}")

    print("\n" + "="*80)
    print("配置完成")
    print("="*80)

    print("\n注意:")
    print("  1. 这些设置只在当前 Python 进程有效")
    print("  2. Quality Session 需要重启才能读取新的环境变量")
    print("  3. 建议将环境变量添加到系统配置或 .env 文件")

if __name__ == '__main__':
    main()
