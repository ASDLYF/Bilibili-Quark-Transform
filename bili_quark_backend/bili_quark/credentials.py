"""凭据读取：环境变量优先，文件回退。只新增，不改动 bili_api.load_cookie 的签名。

- 环境变量名：BILI_COOKIE / QUARK_COOKIE
- 回退文件：bili_cookie.txt / quark_cookie.txt（默认同 workdir，可显式指定）
- 夸克 cookie 必须含 __puus，否则调用方报错退出
"""
import os

from bili_api import load_cookie


class CredentialError(Exception):
    """凭据缺失或格式不对。"""


def _split_lines(value):
    """把多行 cookie 规整成一行；忽略空行与 # 开头的注释行。"""
    return ' '.join(l.strip() for l in value.splitlines()
                    if l.strip() and not l.strip().startswith('#'))


def load_credential(env_name, file_path):
    """环境变量优先，文件回退。

    :param env_name: 环境变量名，例如 'BILI_COOKIE'
    :param file_path: 回退 cookie 文件路径
    :return: cookie 字符串（已去掉换行/注释行）
    :raises CredentialError: 两处都拿不到内容
    """
    v = os.environ.get(env_name)
    if v and v.strip():
        return _split_lines(v)
    if not file_path or not os.path.exists(file_path):
        raise CredentialError(
            '凭据缺失：环境变量 %s 未设置，回退文件也不存在：%s' % (env_name, file_path))
    try:
        return load_cookie(file_path)          # 复用现有实现
    except OSError as e:
        raise CredentialError('读取 cookie 文件失败 %s：%s' % (file_path, e))


def load_bili(workdir):
    """B 站 cookie：BILI_COOKIE 优先，否则 <workdir>/bili_cookie.txt。"""
    return load_credential('BILI_COOKIE', os.path.join(workdir, 'bili_cookie.txt'))


def load_quark(workdir):
    """夸克 cookie：QUARK_COOKIE 优先，否则 <workdir>/quark_cookie.txt。

    校验必须含 __puus（原有行为：缺了就报错退出）。
    """
    cookie = load_credential('QUARK_COOKIE', os.path.join(workdir, 'quark_cookie.txt'))
    if '__puus' not in cookie:
        raise CredentialError(
            '夸克 cookie 无效：必须包含 __puus（来源：环境变量 QUARK_COOKIE '
            '或 %s）' % os.path.join(workdir, 'quark_cookie.txt'))
    return cookie


def credential_source(env_name, file_path):
    """返回凭据来源描述，仅用于日志/诊断。"""
    v = os.environ.get(env_name)
    if v and v.strip():
        return 'env:%s' % env_name
    return 'file:%s' % file_path
