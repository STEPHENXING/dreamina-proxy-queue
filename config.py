"""config.py — 加载 YAML 配置文件，提供全局 cfg 对象。"""

import os
import yaml

_CFG = None


class _AttrDict(dict):
    """字典，支持 d.key 访问（嵌套）。"""

    def __getattr__(self, key):
        try:
            val = self[key]
        except KeyError:
            raise AttributeError(key)
        return val

    def __setattr__(self, key, value):
        self[key] = value


def _to_attr_dict(obj):
    if isinstance(obj, dict):
        return _AttrDict({k: _to_attr_dict(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_attr_dict(i) for i in obj]
    return obj


def load_config(path=None):
    """加载配置文件。*path* 缺省时使用项目根目录下的 ``config.yaml``。"""
    global _CFG
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    _CFG = _to_attr_dict(raw)
    # 确保 data_dir 为绝对路径
    if not os.path.isabs(_CFG.data_dir):
        _CFG.data_dir = os.path.join(os.path.dirname(os.path.abspath(path)), _CFG.data_dir)
    return _CFG


def get_config():
    """获取已加载的配置；若未加载则自动加载默认路径。"""
    global _CFG
    if _CFG is None:
        load_config()
    return _CFG
