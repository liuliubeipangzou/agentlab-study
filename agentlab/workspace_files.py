"""Small local file exchange for the workbench; no model or browser paths are trusted.

Only normal, non-hidden files beneath the configured workspace are exposed. The
directory descriptor stays open across every operation; symlinks are never followed.
Uploads create new files and never replace an existing user file.
"""
import base64
import binascii
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import uuid


MAX_UPLOAD_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024


def _parts(path):
    if (not isinstance(path, str) or not path or len(path) > 1024
            or "\\" in path or "\x00" in path or path.startswith("/")):
        raise ValueError("请使用工作区内的相对路径")
    parts = path.split("/")
    if any(not part or part.startswith(".") or any(ord(c) < 32 for c in part) for part in parts):
        raise ValueError("不允许隐藏文件、上级路径或空路径片段")
    return parts


@contextmanager
def _directory(workspace, parts=()):
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("当前系统不支持安全工作区文件操作")
    descriptor = None
    try:
        descriptor = os.open(str(Path(workspace).resolve()), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError:
        raise ValueError("工作区文件不可访问；请检查文件是否存在且不是符号链接") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def list_workspace(workspace, max_entries=200, max_depth=4):
    if type(max_entries) is not int or not 1 <= max_entries <= 500:
        raise ValueError("文件数量上限必须为 1 至 500")
    if type(max_depth) is not int or not 1 <= max_depth <= 6:
        raise ValueError("目录深度必须为 1 至 6")
    rows, truncated = [], False

    def visit(directory, prefix, depth):
        nonlocal truncated
        # scandir streams entries: even a directory with millions of entries stays bounded.
        with os.scandir(directory) as entries:
            examined = 0
            for entry in entries:
                examined += 1
                if len(rows) >= max_entries or examined > max_entries * 4:
                    truncated = True
                    break
                if entry.name.startswith("."):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    is_dir = stat.S_ISDIR(info.st_mode)
                    if not is_dir and not stat.S_ISREG(info.st_mode):
                        continue
                    path = prefix + entry.name
                    rows.append({"path": path, "name": entry.name,
                                 "kind": "directory" if is_dir else "file",
                                 "size": 0 if is_dir else info.st_size})
                    if is_dir:
                        if depth >= max_depth:
                            truncated = True
                            continue
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                        dir_fd=directory)
                        try:
                            visit(child, path + "/", depth + 1)
                        finally:
                            os.close(child)
                except OSError:
                    # Another process may remove/replace an entry during enumeration.
                    continue

    with _directory(workspace) as directory:
        visit(directory, "", 1)
    return {"files": sorted(rows, key=lambda row: row["path"]), "truncated": truncated,
            "max_file_bytes": MAX_UPLOAD_BYTES, "max_download_bytes": MAX_DOWNLOAD_BYTES}


def import_files(workspace, payload):
    """上传新文件；同名不覆盖，且任一步失败都会回滚本次已创建的文件。

    回滚很重要：否则"第二个文件重名"这类失败会留下第一个文件，而调用方只收到
    错误、以为什么都没发生，工作区就出现了没人认领的文件。
    """
    items = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise ValueError("每次请选择 1 至 10 个文件")
    # base64 编码后长度上界：每 3 字节产生 4 个字符，末尾可能补齐。
    max_encoded = 4 * (MAX_UPLOAD_BYTES // 3 + 1)
    prepared, names, total = [], set(), 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("文件参数无效")
        name = item.get("name")
        parts = _parts(name)
        if len(parts) != 1 or len(name.encode("utf-8")) > 240 or name in names:
            raise ValueError("上传文件名不能含目录、重复或超过 240 字节")
        names.add(name)
        encoded = item.get("content_base64")
        if not isinstance(encoded, str) or len(encoded) > max_encoded:
            raise ValueError("每份上传文件不得超过 1 MiB")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("文件编码无效") from None
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("每份上传文件不得超过 1 MiB")
        total += len(content)
        if total > MAX_UPLOAD_BYTES:
            raise ValueError("一次上传的总大小不得超过 1 MiB")
        prepared.append((name, content))
    created = []
    with _directory(workspace) as directory:
        try:
            for name, content in prepared:
                temporary = ".upload-" + uuid.uuid4().hex
                try:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=directory)
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    # 硬链接提供原子的"不覆盖"发布，且在竞态下也安全。
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                    created.append(name)
                except FileExistsError:
                    raise ValueError("文件已存在，未覆盖：" + name) from None
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
        except BaseException:
            # 回滚本次已发布的文件，避免留下调用方不知情的半成品。
            for name in created:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
            raise
    return {"files": [{"path": name, "size": len(content)} for name, content in prepared]}


def read_file(workspace, path):
    parts = _parts(path)
    with _directory(workspace, parts[:-1]) as directory:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("只能下载普通文件")
            if info.st_size > MAX_DOWNLOAD_BYTES:
                raise ValueError("页面下载上限为 5 MiB，请从本机 workspace 目录读取")
            content = source.read(MAX_DOWNLOAD_BYTES + 1)
            if len(content) > MAX_DOWNLOAD_BYTES:
                raise ValueError("文件超过 5 MiB 下载上限")
    return {"name": parts[-1], "content_base64": base64.b64encode(content).decode("ascii"),
            "content_type": "application/octet-stream", "size": len(content)}
