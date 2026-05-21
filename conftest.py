"""
pytest 配置文件
- 将项目根目录加入 sys.path，使所有测试可直接 import 项目模块
"""
import sys
import os

# 确保项目根目录在 sys.path 中
ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from config.xtquant_bootstrap import bootstrap_xtquant_sys_path

bootstrap_xtquant_sys_path()
