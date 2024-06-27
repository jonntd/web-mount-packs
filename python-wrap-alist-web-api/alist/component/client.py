#!/usr/bin/env python3
# encoding: utf-8

"""Python AList web api wrapper.

This is a web api wrapper works with the running "alist" server, and provide some methods, 
which refer to `os`, `posixpath`, `pathlib.Path` and `shutil` modules.

- AList web api official documentation: https://alist.nn.ci/guide/api/
- AList web api online tool: https://alist-v3.apifox.cn
"""

from __future__ import annotations

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = ["AlistClient", "check_response"]

import errno

from collections.abc import Awaitable, Callable, Mapping
from functools import cached_property, partial, update_wrapper
from hashlib import sha256
from http.cookiejar import CookieJar
from inspect import isawaitable
from io import TextIOWrapper
from json import loads
from os import fstat, PathLike
from typing import overload, Literal
from types import MethodType
from urllib.parse import quote
from warnings import filterwarnings

from dateutil.parser import parse as dt_parse
from filewrap import bio_chunk_iter, bio_chunk_async_iter, SupportsRead
from httpfile import HTTPFileReader
from http_request import complete_url, encode_multipart_data, encode_multipart_data_async
from http_response import get_content_length
from httpx import AsyncClient, Client, Cookies, AsyncHTTPTransport, HTTPTransport
from httpx_request import request
from iterutils import run_gen_step
from multidict import CIMultiDict


filterwarnings("ignore", category=DeprecationWarning)
parse_json = lambda _, content: loads(content)
httpx_request = partial(request, timeout=(5, 60, 60, 5))


class method:

    def __init__(self, func: Callable, /):
        self.__func__ = func

    def __get__(self, instance, type=None, /):
        if instance is None:
            return self
        return MethodType(self.__func__, instance)

    def __set__(self, instance, value, /):
        raise TypeError("can't set value")


def check_response(func: dict | Awaitable[dict] | Callable):
    def check_code(resp):
        code = resp["code"]
        if 200 <= code < 300:
            return resp
        elif code == 403:
            raise PermissionError(errno.EACCES, resp)
        elif code == 500:
            message = resp["message"]
            if (message.endswith("object not found") 
                or message.startswith("failed get storage: storage not found")
            ):
                raise FileNotFoundError(errno.ENOENT, resp)
            elif resp["message"].endswith("not a folder"):
                raise NotADirectoryError(errno.ENOTDIR, resp)
            elif message.endswith("file exists"):
                raise FileExistsError(errno.EEXIST, resp)
            elif message.startswith("failed get "):
                raise PermissionError(errno.EPERM, resp)
        raise OSError(errno.EIO, resp)
    async def check_code_async(resp):
        return check_code(await resp)
    if callable(func):
        def wrapper(*args, **kwds):
            resp = func(*args, **kwds)
            if isawaitable(resp):
                return check_code_async(resp)
            return check_code(resp)
        return update_wrapper(wrapper, func)
    elif isawaitable(func):
        return check_code_async(func)
    else:
        return check_code(func)


def parse_as_timestamp(s: None | str = None, /) -> float:
    if not s:
        return 0.0
    if s.startswith("0001-01-01"):
        return 0.0
    try:
        return dt_parse(s).timestamp()
    except:
        return 0.0


class AlistClient:
    """AList client that encapsulates web APIs

    - AList web api official documentation: https://alist.nn.ci/guide/api/
    - AList web api online tool: https://alist-v3.apifox.cn
    """
    origin: str
    username: str
    password: str
    otp_code: str

    def __init__(
        self, 
        /, 
        origin: str = "http://localhost:5244", 
        username: str = "", 
        password: str = "", 
        otp_code: int | str = "", 
    ):
        self.__dict__.update(
            origin=complete_url(origin), 
            username=username, 
            password=password, 
            otp_code=otp_code, 
            headers = CIMultiDict({
                "Accept": "application/json, text/plain, */*", 
                "Accept-Encoding": "gzip, deflate, br, zstd", 
                "Connection": "keep-alive", 
            }), 
            cookies = Cookies(), 
        )
        if username:
            self.login()

    def __del__(self, /):
        self.close()

    def __eq__(self, other, /) -> bool:
        return type(self) is type(other) and self.origin == other.origin and self.username == other.username

    def __repr__(self, /) -> str:
        cls = type(self)
        module = cls.__module__
        name = cls.__qualname__
        if module != "__main__":
            name = module + "." + name
        return f"{name}(origin={self.origin!r}, username={self.username!r}, password='******')"

    def __setattr__(self, attr, val, /):
        if attr in ("username", "password", "otp_code"):
            self.__dict__[attr] = str(val)
            if attr != "username":
                self.login()
        raise TypeError(f"can't set attribute: {attr!r}")

    @cached_property
    def base_path(self, /) -> str:
        return self.get_base_path()

    @cached_property
    def session(self, /) -> Client:
        """同步请求的 session
        """
        ns = self.__dict__
        session = Client(transport=HTTPTransport(retries=5), verify=False)
        session._headers = ns["headers"]
        session._cookies = ns["cookies"]
        return session

    @cached_property
    def async_session(self, /) -> AsyncClient:
        """异步请求的 session
        """
        ns = self.__dict__
        session = AsyncClient(transport=AsyncHTTPTransport(retries=5), verify=False)
        session._headers = ns["headers"]
        session._cookies = ns["cookies"]
        return session

    @property
    def cookiejar(self, /) -> CookieJar:
        return self.__dict__["cookies"].jar

    @property
    def headers(self, /) -> CIMultiDict:
        """请求头，无论同步还是异步请求都共用这个请求头
        """
        return self.__dict__["headers"]

    @headers.setter
    def headers(self, headers, /):
        """替换请求头，如果需要更新，请用 <client>.headers.update
        """
        headers = CIMultiDict(headers)
        default_headers = self.headers
        default_headers.clear()
        default_headers.update(headers)

    def close(self, /) -> None:
        """删除 session 和 async_session，如果它们未被引用，则会被自动清理
        """
        ns = self.__dict__
        ns.pop("session", None)
        ns.pop("async_session", None)

    def request(
        self, 
        /, 
        url: str, 
        method: str = "POST", 
        async_: Literal[False, True] = False, 
        request: None | Callable = None, 
        **request_kwargs, 
    ):
        """执行 http 请求，默认为 POST 方法（因为 alist 的大部分 web api 是 POST 的）
        在线 API 文档：https://alist-v3.apifox.cn
        """
        if not url.startswith(("http://", "https://")):
            if not url.startswith("/"):
                url = "/" + url
            url = self.origin + url
        request_kwargs.setdefault("parse", parse_json)
        if request is None:
            request_kwargs["session"] = self.async_session if async_ else self.session
            return httpx_request(
                url=url, 
                method=method, 
                async_=async_, 
                **request_kwargs, 
            )
        else:
            if (headers := request_kwargs.get("headers")):
                request_kwargs["headers"] = {**self.headers, **headers}
            else:
                request_kwargs["headers"] = self.headers
            return request(
                url=url, 
                method=method, 
                **request_kwargs, 
            )

    def login(
        self, 
        /, 
        username: str = "", 
        password: str = "", 
        otp_code: int | str = "", 
        hash_password: bool = True, 
        *, 
        async_: Literal[False, True] = False, 
        **request_kwargs, 
    ):
        ns = self.__dict__
        if username:
            ns["username"] = username
        else:
            username = ns["username"]
        if password:
            ns["password"] = password
        else:
            password = ns["password"]
        if otp_code:
            ns["otp_code"] = otp_code
        else:
            otp_code = ns["otp_code"]
        def gen_step():
            if username:
                if hash_password:
                    method = self.auth_login_hash
                    payload = {
                        "username": username, 
                        "password": sha256(f"{password}-https://github.com/alist-org/alist".encode("utf-8")).hexdigest(), 
                        "otp_code": otp_code, 
                    }
                else:
                    method = self.auth_login
                    payload = {"username": username, "password": password, "otp_code": otp_code}
                resp = yield partial(
                    method, 
                    payload, 
                    async_=async_, 
                    **request_kwargs, 
                )
                if not 200 <= resp["code"] < 300:
                    raise OSError(errno.EINVAL, resp)
                self.headers["Authorization"] = resp["data"]["token"]
            else:
                self.headers.pop("Authorization", None)
            ns.pop("base_path", None)
        return run_gen_step(gen_step, async_=async_)

    @overload
    def get_base_path(
        self, 
        /, 
        async_: Literal[False] = False, 
    ) -> str | Awaitable[str]:
        ...
    @overload
    def get_base_path(
        self, 
        /, 
        async_: Literal[True], 
    ) -> Awaitable[str]:
        ...
    def get_base_path(
        self, 
        /, 
        async_: Literal[False, True] = False, 
    ) -> str | Awaitable[str]:
        def gen_step():
            resp = yield partial(self.auth_me, async_=async_)
            return resp["data"]["base_path"]
        return run_gen_step(gen_step, async_=async_)

    # [auth](https://alist.nn.ci/guide/api/auth.html)

    def auth_login(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """token获取
        - https://alist.nn.ci/guide/api/auth.html#post-token获取
        - https://alist-v3.apifox.cn/api-128101241
        """
        return self.request(
            "/api/auth/login", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def auth_login_hash(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """token获取hash
        - https://alist.nn.ci/guide/api/auth.html#post-token获取hash
        - https://alist-v3.apifox.cn/api-128101242
        """
        return self.request(
            "/api/auth/login/hash", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def auth_2fa_generate(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """生成2FA密钥
        - https://alist.nn.ci/guide/api/auth.html#post-生成2fa密钥
        - https://alist-v3.apifox.cn/api-128101243
        """
        return self.request(
            "/api/auth/2fa/generate", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def auth_2fa_verify(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """验证2FA code
        - https://alist.nn.ci/guide/api/auth.html#post-验证2fa-code
        - https://alist-v3.apifox.cn/api-128101244
        """
        return self.request(
            "/api/auth/2fa/verify", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def auth_me(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取当前用户信息
        - https://alist.nn.ci/guide/api/auth.html#get-获取当前用户信息
        - https://alist-v3.apifox.cn/api-128101245
        """
        return self.request(
            "/api/me", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def auth_me_update(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """更新当前用户信息
        """
        return self.request(
            "/api/me/update", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    # [fs](https://alist.nn.ci/guide/api/fs.html)

    def fs_list(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """列出文件目录
        - https://alist.nn.ci/guide/api/fs.html#post-列出文件目录
        - https://alist-v3.apifox.cn/api-128101246
        """
        return self.request(
            "/api/fs/list", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_get(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取某个文件/目录信息
        - https://alist.nn.ci/guide/api/fs.html#post-获取某个文件-目录信息
        - https://alist-v3.apifox.cn/api-128101247
        """
        return self.request(
            "/api/fs/get", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_dirs(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取目录
        - https://alist.nn.ci/guide/api/fs.html#post-获取目录
        - https://alist-v3.apifox.cn/api-128101248
        """
        return self.request(
            "/api/fs/dirs", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_search(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """搜索文件或文件夹
        - https://alist.nn.ci/guide/api/fs.html#post-搜索文件或文件夹
        - https://alist-v3.apifox.cn/api-128101249
        """
        return self.request(
            "/api/fs/search", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_mkdir(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """新建文件夹
        - https://alist.nn.ci/guide/api/fs.html#post-新建文件夹
        - https://alist-v3.apifox.cn/api-128101250
        """
        return self.request(
            "/api/fs/mkdir", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_rename(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """重命名文件
        - https://alist.nn.ci/guide/api/fs.html#post-重命名文件
        - https://alist-v3.apifox.cn/api-128101251

        NOTE: AList 改名的限制：
        1. 受到网盘的改名限制，例如如果挂载的是 115，就不能包含特殊符号 " < > ，也不能改扩展名，各个网盘限制不同
        2. 可以包含斜杠  \，但是改名后，这个文件不能被删改了，因为只能被罗列，但不能单独找到
        3. 名字里（basename）中包含 /，会被替换为 |
        """
        return self.request(
            "/api/fs/rename", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_batch_rename(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """批量重命名
        - https://alist.nn.ci/guide/api/fs.html#post-批量重命名
        - https://alist-v3.apifox.cn/api-128101252
        """
        return self.request(
            "/api/fs/batch_rename", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_regex_rename(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """正则重命名
        - https://alist.nn.ci/guide/api/fs.html#post-正则重命名
        - https://alist-v3.apifox.cn/api-128101253
        """
        return self.request(
            "/api/fs/regex_rename", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_move(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """移动文件
        - https://alist.nn.ci/guide/api/fs.html#post-移动文件
        - https://alist-v3.apifox.cn/api-128101255
        """
        return self.request(
            "/api/fs/move", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_recursive_move(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """聚合移动
        - https://alist.nn.ci/guide/api/fs.html#post-聚合移动
        - https://alist-v3.apifox.cn/api-128101259
        """
        return self.request(
            "/api/fs/recursive_move", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_copy(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """复制文件
        - https://alist.nn.ci/guide/api/fs.html#post-复制文件
        - https://alist-v3.apifox.cn/api-128101256
        """
        return self.request(
            "/api/fs/copy", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_remove(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """删除文件或文件夹
        - https://alist.nn.ci/guide/api/fs.html#post-删除文件或文件夹
        - https://alist-v3.apifox.cn/api-128101257
        """
        return self.request(
            "/api/fs/remove", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_remove_empty_directory(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """删除空文件夹
        - https://alist.nn.ci/guide/api/fs.html#post-删除空文件夹
        - https://alist-v3.apifox.cn/api-128101258
        """
        return self.request(
            "/api/fs/remove_empty_directory", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def fs_add_offline_download(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """添加离线下载
        - https://alist.nn.ci/guide/api/fs.html#post-添加离线下载
        - https://alist-v3.apifox.cn/api-175404336
        """
        return self.request(
            "/api/fs/add_offline_download", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    # TODO: local_path_or_file 需要和 p115 协调
    def fs_form(
        self, 
        local_path_or_file: bytes | str | PathLike | SupportsRead[bytes] | TextIOWrapper, 
        /, 
        remote_path: str, 
        as_task: bool = False, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """表单上传文件
        - https://alist.nn.ci/guide/api/fs.html#put-表单上传文件
        - https://alist-v3.apifox.cn/api-128101254
        """
        if headers := request_kwargs.get("headers"):
            headers = {**headers, "File-Path": quote(remote_path)}
        else:
            headers = {"File-Path": quote(remote_path)}
        request_kwargs["headers"] = headers
        if as_task:
            headers["As-Task"] = "true"
        if hasattr(local_path_or_file, "read"):
            file = local_path_or_file
            if isinstance(file, TextIOWrapper):
                file = file.buffer
        else:
            file = open(local_path_or_file, "rb")
        if async_:
           update_headers, data = encode_multipart_data_async({}, {"file": file})
        else:
           update_headers, data = encode_multipart_data({}, {"file": file})
        headers.update(update_headers)
        request_kwargs["data"] = data
        return self.request(
            "/api/fs/form", 
            "PUT", 
            async_=async_, 
            **request_kwargs, 
        )

    # TODO: local_path_or_file 需要和 p115 协调
    def fs_put(
        self, 
        local_path_or_file: bytes | str | PathLike | SupportsRead[bytes] | TextIOWrapper, 
        /, 
        remote_path: str, 
        as_task: bool = False, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """流式上传文件
        - https://alist.nn.ci/guide/api/fs.html#put-流式上传文件
        - https://alist-v3.apifox.cn/api-128101260

        NOTE: AList 上传的限制：
        1. 上传文件成功不会自动更新缓存，但新增文件夹会更新缓存
        2. 上传时路径中包含斜杠 \\，视为路径分隔符 /
        3. put 接口是流式上传，但是不支持 chunked（所以需要在上传前，就能直接确定总上传的字节数）
        """
        if headers := request_kwargs.get("headers"):
            headers = {**headers, "File-Path": quote(remote_path)}
        else:
            headers = {"File-Path": quote(remote_path)}
        request_kwargs["headers"] = headers
        if as_task:
            headers["As-Task"] = "true"
        if hasattr(local_path_or_file, "read"):
            file = local_path_or_file
            if isinstance(file, TextIOWrapper):
                file = file.buffer
        else:
            file = open(local_path_or_file, "rb")
        headers["Content-Length"] = str(fstat(file.fileno()).st_size)
        if async_:
            request_kwargs["data"] = bio_chunk_async_iter(file)
        else:
            request_kwargs["data"] = bio_chunk_iter(file)
        return self.request(
            "/api/fs/put", 
            "PUT", 
            async_=async_, 
            **request_kwargs, 
        )

    # [public](https://alist.nn.ci/guide/api/public.html)

    def public_settings(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取站点设置
        - https://alist.nn.ci/guide/api/public.html#get-获取站点设置
        - https://alist-v3.apifox.cn/api-128101263
        """
        return self.request(
            "/api/public/settings", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def public_ping(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> str:
        """ping检测
        - https://alist.nn.ci/guide/api/public.html#get-ping检测
        - https://alist-v3.apifox.cn/api-128101264
        """
        return self.request(
            "/ping", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    # [admin](https://alist.nn.ci/guide/api/admin/)

    # [admin/meta](https://alist.nn.ci/guide/api/admin/meta.html)

    def admin_meta_list(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """列出元信息
        - https://alist.nn.ci/guide/api/admin/meta.html#get-列出元信息
        - https://alist-v3.apifox.cn/api-128101265
        """
        return self.request(
            "/api/admin/meta/list", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_meta_get(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取元信息
        - https://alist.nn.ci/guide/api/admin/meta.html#get-获取元信息
        - https://alist-v3.apifox.cn/api-128101266
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/meta/get", 
            "GET", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_meta_create(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """新增元信息
        - https://alist.nn.ci/guide/api/admin/meta.html#post-新增元信息
        - https://alist-v3.apifox.cn/api-128101267
        """
        return self.request(
            "/api/admin/meta/create", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_meta_update(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """更新元信息
        - https://alist.nn.ci/guide/api/admin/meta.html#post-更新元信息
        - https://alist-v3.apifox.cn/api-128101268
        """
        return self.request(
            "/api/admin/meta/update", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_meta_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """删除元信息
        - https://alist.nn.ci/guide/api/admin/meta.html#post-删除元信息
        - https://alist-v3.apifox.cn/api-128101269
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/meta/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    # [admin/user](https://alist.nn.ci/guide/api/admin/user.html)

    def admin_user_list(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """列出所有用户
        - https://alist.nn.ci/guide/api/admin/user.html#get-列出所有用户
        - https://alist-v3.apifox.cn/api-128101270
        """
        return self.request(
            "/api/admin/user/list", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_user_get(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """列出某个用户
        - https://alist.nn.ci/guide/api/admin/user.html#get-列出某个用户
        - https://alist-v3.apifox.cn/api-128101271
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/user/get", 
            "GET", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_user_create(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """新建用户
        - https://alist.nn.ci/guide/api/admin/user.html#post-新建用户
        - https://alist-v3.apifox.cn/api-128101272
        """
        return self.request(
            "/api/admin/user/create", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_user_update(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """更新用户信息
        - https://alist.nn.ci/guide/api/admin/user.html#post-更新用户信息
        - https://alist-v3.apifox.cn/api-128101273
        """
        return self.request(
            "/api/admin/user/update", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_user_cancel_2fa(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """取消某个用户的两步验证
        - https://alist.nn.ci/guide/api/admin/user.html#post-取消某个用户的两步验证
        - https://alist-v3.apifox.cn/api-128101274
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/user/cancel_2fa", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_user_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """删除用户
        - https://alist.nn.ci/guide/api/admin/user.html#post-删除用户
        - https://alist-v3.apifox.cn/api-128101275
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/user/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_user_del_cache(
        self, 
        /, 
        payload: str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """删除用户缓存
        - https://alist.nn.ci/guide/api/admin/user.html#post-删除用户缓存
        - https://alist-v3.apifox.cn/api-128101276
        """
        if isinstance(payload, str):
            payload = {"username": payload}
        return self.request(
            "/api/admin/user/del_cache", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    # [admin/storage](https://alist.nn.ci/guide/api/admin/storage.html)

    def admin_storage_create(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """创建存储
        - https://alist.nn.ci/guide/api/admin/storage.html#post-创建存储
        - https://alist-v3.apifox.cn/api-175457115
        """
        return self.request(
            "/api/admin/storage/create", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_storage_update(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """更新存储
        - https://alist.nn.ci/guide/api/admin/storage.html#post-更新存储
        - https://alist-v3.apifox.cn/api-175457877
        """
        return self.request(
            "/api/admin/storage/update", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_storage_list(
        self, 
        /, 
        payload: dict = {"page": 1, "per_page": 0}, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """列出存储列表
        - https://alist.nn.ci/guide/api/admin/storage.html#get-列出存储列表
        - https://alist-v3.apifox.cn/api-128101277
        """
        return self.request(
            "/api/admin/storage/list", 
            "GET", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_storage_enable(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """启用存储
        - https://alist.nn.ci/guide/api/admin/storage.html#post-启用存储
        - https://alist-v3.apifox.cn/api-128101278
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/storage/enable", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_storage_disable(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """禁用存储
        - https://alist.nn.ci/guide/api/admin/storage.html#post-禁用存储
        - https://alist-v3.apifox.cn/api-128101279
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/storage/disable", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_storage_get(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """查询指定存储信息
        - https://alist.nn.ci/guide/api/admin/storage.html#get-查询指定存储信息
        - https://alist-v3.apifox.cn/api-128101281
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/storage/get", 
            "GET", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_storage_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """删除指定存储
        - https://alist.nn.ci/guide/api/admin/storage.html#post-删除指定存储
        - https://alist-v3.apifox.cn/api-128101282
        """
        if isinstance(payload, (int, str)):
            payload = {"id": payload}
        return self.request(
            "/api/admin/storage/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_storage_load_all(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """重新加载所有存储
        - https://alist.nn.ci/guide/api/admin/storage.html#post-重新加载所有存储
        - https://alist-v3.apifox.cn/api-128101283
        """
        return self.request(
            "/api/admin/storage/load_all", 
            async_=async_, 
            **request_kwargs, 
        )

    # [admin/driver](https://alist.nn.ci/guide/api/admin/driver.html)

    def admin_driver_list(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """查询所有驱动配置模板列表
        - https://alist.nn.ci/guide/api/admin/driver.html#get-查询所有驱动配置模板列表
        - https://alist-v3.apifox.cn/api-128101284
        """
        return self.request(
            "/api/admin/driver/list", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_driver_names(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """列出驱动名列表
        - https://alist.nn.ci/guide/api/admin/driver.html#get-列出驱动名列表
        - https://alist-v3.apifox.cn/api-128101285
        """
        return self.request(
            "/api/admin/driver/names", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_driver_info(
        self, 
        /, 
        payload: str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """列出特定驱动信息
        - https://alist.nn.ci/guide/api/admin/driver.html#get-列出特定驱动信息
        - https://alist-v3.apifox.cn/api-128101286
        """
        if isinstance(payload, str):
            payload = {"driver": payload}
        return self.request(
            "/api/admin/driver/info", 
            "GET", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    # [admin/setting](https://alist.nn.ci/guide/api/admin/setting.html)

    def admin_setting_list(
        self, 
        /, 
        payload: dict = {}, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """列出设置
        - https://alist.nn.ci/guide/api/admin/setting.html#get-列出设置
        - https://alist-v3.apifox.cn/api-128101287
        """
        return self.request(
            "/api/admin/setting/list", 
            "GET", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_setting_get(
        self, 
        /, 
        payload: dict = {}, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取某项设置
        - https://alist.nn.ci/guide/api/admin/setting.html#get-获取某项设置
        - https://alist-v3.apifox.cn/api-128101288
        """
        return self.request(
            "/api/admin/setting/get", 
            "GET", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_setting_save(
        self, 
        /, 
        payload: list[dict], 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """保存设置
        - https://alist.nn.ci/guide/api/admin/setting.html#post-保存设置
        - https://alist-v3.apifox.cn/api-128101289
        """
        return self.request(
            "/api/admin/setting/save", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_setting_delete(
        self, 
        /, 
        payload: str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """删除设置
        - https://alist.nn.ci/guide/api/admin/setting.html#post-删除设置
        - https://alist-v3.apifox.cn/api-128101290
        """
        if isinstance(payload, str):
            payload = {"key": payload}
        return self.request(
            "/api/admin/setting/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_setting_reset_token(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """重置令牌
        - https://alist.nn.ci/guide/api/admin/setting.html#post-重置令牌
        - https://alist-v3.apifox.cn/api-128101291
        """
        return self.request(
            "/api/admin/setting/reset_token", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_setting_set_aria2(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """设置aria2
        - https://alist.nn.ci/guide/api/admin/setting.html#post-设置aria2
        - https://alist-v3.apifox.cn/api-128101292
        """
        return self.request(
            "/api/admin/setting/set_aria2", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_setting_set_qbit(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """设置qBittorrent
        - https://alist.nn.ci/guide/api/admin/setting.html#post-设置qbittorrent
        - https://alist-v3.apifox.cn/api-128101293
        """
        return self.request(
            "/api/admin/setting/set_qbit", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    # [admin/task](https://alist.nn.ci/guide/api/admin/task.html)

    def admin_task_upload_info(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取任务信息
        - https://alist.nn.ci/guide/api/admin/task.html#post-获取任务信息
        - https://alist-v3.apifox.cn/api-142468741
        """
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/upload/info", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_upload_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取已完成任务
        - https://alist.nn.ci/guide/api/admin/task.html#get-获取已完成任务
        - https://alist-v3.apifox.cn/api-128101294
        """
        return self.request(
            "/api/admin/task/upload/done", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_upload_undone(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """获取未完成任务
        - https://alist.nn.ci/guide/api/admin/task.html#get-获取未完成任务
        - https://alist-v3.apifox.cn/api-128101295
        """
        return self.request(
            "/api/admin/task/upload/undone", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_upload_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """删除任务
        - https://alist.nn.ci/guide/api/admin/task.html#post-删除任务
        - https://alist-v3.apifox.cn/api-128101296
        """
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/upload/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_upload_cancel(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """取消任务
        - https://alist.nn.ci/guide/api/admin/task.html#post-取消任务
        - https://alist-v3.apifox.cn/api-128101297
        """
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/upload/cancel", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_upload_retry(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """重试任务
        - https://alist.nn.ci/guide/api/admin/task.html#post-重试任务
        - https://alist-v3.apifox.cn/api-128101298
        """
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/upload/retry", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_upload_retry_failed(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """重试已失败任务
        - https://alist.nn.ci/guide/api/admin/task.html#post-重试已失败任务
        """
        return self.request(
            "/api/admin/task/upload/retry_failed", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_upload_clear_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """清除已完成任务
        - https://alist.nn.ci/guide/api/admin/task.html#post-清除已完成任务
        - https://alist-v3.apifox.cn/api-128101299
        """
        return self.request(
            "/api/admin/task/upload/clear_done", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_upload_clear_succeeded(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        """清除已成功任务
        - https://alist.nn.ci/guide/api/admin/task.html#post-清除已成功任务
        - https://alist-v3.apifox.cn/api-128101300
        """
        return self.request(
            "/api/admin/task/upload/clear_succeeded", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_info(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-获取任务信息"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/copy/info", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取已完成任务"
        return self.request(
            "/api/admin/task/copy/done", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_undone(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取未完成任务"
        return self.request(
            "/api/admin/task/copy/undone", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-删除任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/copy/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_cancel(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-取消任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/copy/cancel", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_retry(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/copy/retry", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_retry_failed(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试已失败任务"
        return self.request(
            "/api/admin/task/copy/retry_failed", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_clear_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已完成任务"
        return self.request(
            "/api/admin/task/copy/clear_done", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_copy_clear_succeeded(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已成功任务"
        return self.request(
            "/api/admin/task/copy/clear_succeeded", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_info(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-获取任务信息"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/aria2_down/info", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取已完成任务"
        return self.request(
            "/api/admin/task/aria2_down/done", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_undone(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取未完成任务"
        return self.request(
            "/api/admin/task/aria2_down/undone", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-删除任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/aria2_down/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_cancel(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-取消任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/aria2_down/cancel", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_retry(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/aria2_down/retry", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_retry_failed(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试已失败任务"
        return self.request(
            "/api/admin/task/aria2_down/retry_failed", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_clear_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已完成任务"
        return self.request(
            "/api/admin/task/aria2_down/clear_done", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_down_clear_succeeded(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已成功任务"
        return self.request(
            "/api/admin/task/aria2_down/clear_succeeded", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_info(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-获取任务信息"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/aria2_transfer/info", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取已完成任务"
        return self.request(
            "/api/admin/task/aria2_transfer/done", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_undone(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取未完成任务"
        return self.request(
            "/api/admin/task/aria2_transfer/undone", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-删除任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/aria2_transfer/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_cancel(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-取消任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/aria2_transfer/cancel", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_retry(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/aria2_transfer/retry", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_retry_failed(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试已失败任务"
        return self.request(
            "/api/admin/task/aria2_transfer/retry_failed", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_clear_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已完成任务"
        return self.request(
            "/api/admin/task/aria2_transfer/clear_done", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_aria2_transfer_clear_succeeded(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已成功任务"
        return self.request(
            "/api/admin/task/aria2_transfer/clear_succeeded", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_info(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-获取任务信息"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/qbit_down/info", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取已完成任务"
        return self.request(
            "/api/admin/task/qbit_down/done", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_undone(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取未完成任务"
        return self.request(
            "/api/admin/task/qbit_down/undone", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-删除任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/qbit_down/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_cancel(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-取消任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/qbit_down/cancel", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_retry(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/qbit_down/retry", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_retry_failed(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试已失败任务"
        return self.request(
            "/api/admin/task/qbit_down/retry_failed", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_clear_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已完成任务"
        return self.request(
            "/api/admin/task/qbit_down/clear_done", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_down_clear_succeeded(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已成功任务"
        return self.request(
            "/api/admin/task/qbit_down/clear_succeeded", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_info(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-获取任务信息"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/qbit_transfer/info", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取已完成任务"
        return self.request(
            "/api/admin/task/qbit_transfer/done", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_undone(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取未完成任务"
        return self.request(
            "/api/admin/task/qbit_transfer/undone", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-删除任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/qbit_transfer/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_cancel(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-取消任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/qbit_transfer/cancel", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_retry(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/qbit_transfer/retry", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_retry_failed(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试已失败任务"
        return self.request(
            "/api/admin/task/qbit_transfer/retry_failed", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_clear_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已完成任务"
        return self.request(
            "/api/admin/task/qbit_transfer/clear_done", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_qbit_transfer_clear_succeeded(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已成功任务"
        return self.request(
            "/api/admin/task/qbit_transfer/clear_succeeded", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_info(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-获取任务信息"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/offline_download/info", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取已完成任务"
        return self.request(
            "/api/admin/task/offline_download/done", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_undone(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取未完成任务"
        return self.request(
            "/api/admin/task/offline_download/undone", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-删除任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/offline_download/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_cancel(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-取消任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/offline_download/cancel", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_retry(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/offline_download/retry", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_retry_failed(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试已失败任务"
        return self.request(
            "/api/admin/task/offline_download/retry_failed", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_clear_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已完成任务"
        return self.request(
            "/api/admin/task/offline_download/clear_done", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_clear_succeeded(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已成功任务"
        return self.request(
            "/api/admin/task/offline_download/clear_succeeded", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_info(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-获取任务信息"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/offline_download_transfer/info", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取已完成任务"
        return self.request(
            "/api/admin/task/offline_download_transfer/done", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_undone(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#get-获取未完成任务"
        return self.request(
            "/api/admin/task/offline_download_transfer/undone", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_delete(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-删除任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/offline_download_transfer/delete", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_cancel(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-取消任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/offline_download_transfer/cancel", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_retry(
        self, 
        /, 
        payload: int | str | dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试任务"
        if isinstance(payload, (int, str)):
            payload = {"tid": payload}
        return self.request(
            "/api/admin/task/offline_download_transfer/retry", 
            params=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_retry_failed(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-重试已失败任务"
        return self.request(
            "/api/admin/task/offline_download_transfer/retry_failed", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_clear_done(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已完成任务"
        return self.request(
            "/api/admin/task/offline_download_transfer/clear_done", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_task_offline_download_transfer_clear_succeeded(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        "https://alist.nn.ci/guide/api/admin/task.html#post-清除已成功任务"
        return self.request(
            "/api/admin/task/offline_download_transfer/clear_succeeded", 
            async_=async_, 
            **request_kwargs, 
        )

    # Undocumented

    def admin_index_progress(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        return self.request(
            "/api/admin/index/progress", 
            "GET", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_index_build(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        return self.request(
            "/api/admin/index/build", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_index_clear(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        return self.request(
            "/api/admin/index/clear", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_index_stop(
        self, 
        /, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        return self.request(
            "/api/admin/index/stop", 
            async_=async_, 
            **request_kwargs, 
        )

    def admin_index_update(
        self, 
        /, 
        payload: dict, 
        async_: bool = False, 
        **request_kwargs, 
    ) -> dict:
        return self.request(
            "/api/admin/index/update", 
            json=payload, 
            async_=async_, 
            **request_kwargs, 
        )

    ########## Other Encapsulations ##########

    def get_url(
        self, 
        /, 
        path: str, 
        ensure_ascii: bool = True, 
    ) -> str:
        if self.base_path != "/":
            path = self.base_path + path
        if ensure_ascii:
            return self.origin + "/d" + quote(path, safe="@[]:/!$&'()*+,;=")
        else:
            return self.origin + "/d" + path.translate({0x23: "%23", 0x3F: "%3F"})

    @staticmethod
    def open(
        url: str | Callable[[], str], 
        headers: None | Mapping = None, 
        start: int = 0, 
        seek_threshold: int = 1 << 20, 
        async_: Literal[False, True] = False, 
    ) -> HTTPFileReader:
        """打开下载链接，返回可读的文件对象
        """
        if async_:
            raise OSError(errno.ENOSYS, "asynchronous mode not implemented")
        return HTTPFileReader(
            url, 
            headers=headers, 
            start=start, 
            seek_threshold=seek_threshold, 
        )

    @overload
    def read_bytes(
        self, 
        /, 
        url: str, 
        start: int = 0, 
        stop: None | int = None, 
        *, 
        async_: Literal[False] = False, 
        **request_kwargs, 
    ) -> bytes:
        ...
    @overload
    def read_bytes(
        self, 
        /, 
        url: str, 
        start: int = 0, 
        stop: None | int = None, 
        *, 
        async_: Literal[True], 
        **request_kwargs, 
    ) -> Awaitable[bytes]:
        ...
    def read_bytes(
        self, 
        /, 
        url: str, 
        start: int = 0, 
        stop: None | int = None, 
        *, 
        async_: Literal[False, True] = False, 
        **request_kwargs, 
    ) -> bytes | Awaitable[bytes]:
        """读取文件一定索引范围的数据
        :param url: 115 文件的下载链接（可以从网盘、网盘上的压缩包内、分享链接中获取）
        :param start: 开始索引，可以为负数（从文件尾部开始）
        :param stop: 结束索引（不含），可以为负数（从文件尾部开始）
        :param async_: 是否异步
        :param request_kwargs: 其它请求参数
        """
        def gen_step():
            def get_bytes_range(start, stop):
                if start < 0 or (stop and stop < 0):
                    if headers := request_kwargs.get("headers"):
                        headers = {**headers, "Accept-Encoding": "identity", "Range": "bytes=-1"}
                    else:
                        headers = {"Accept-Encoding": "identity", "Range": "bytes=-1"}
                    resp = yield partial(
                        self.request, 
                        url, 
                        async_=async_, 
                        **{**request_kwargs, "headers": headers, "parse": None}, 
                    )
                    try:
                        length = get_total_length(resp)
                        if length is None:
                            raise OSError(errno.ESPIPE, "can't determine content length")
                    finally:
                        if async_ and hasattr(resp, "aclose"):
                            yield resp.aclose
                        else:
                            yield resp.close
                    if start < 0:
                        start += length
                    if start < 0:
                        start = 0
                    if stop is None:
                        return f"{start}-"
                    elif stop < 0:
                        stop += length
                if start >= stop:
                    return None
                return f"{start}-{stop-1}"
            bytes_range = yield from get_bytes_range(start, stop)
            if not bytes_range:
                return b""
            return (yield partial(
                self.read_bytes_range, 
                url, 
                bytes_range=bytes_range, 
                async_=async_, 
                **request_kwargs, 
            ))
        return run_gen_step(gen_step, async_=async_)

    @overload
    def read_bytes_range(
        self, 
        /, 
        url: str, 
        bytes_range: str = "0-", 
        headers: None | Mapping = None, 
        *, 
        async_: Literal[False] = False, 
        **request_kwargs, 
    ) -> bytes:
        ...
    @overload
    def read_bytes_range(
        self, 
        /, 
        url: str, 
        bytes_range: str = "0-", 
        headers: None | Mapping = None, 
        *, 
        async_: Literal[True], 
        **request_kwargs, 
    ) -> Awaitable[bytes]:
        ...
    def read_bytes_range(
        self, 
        /, 
        url: str, 
        bytes_range: str = "0-", 
        headers: None | Mapping = None, 
        *, 
        async_: Literal[False, True] = False, 
        **request_kwargs, 
    ) -> bytes | Awaitable[bytes]:
        """读取文件一定索引范围的数据
        :param url: 115 文件的下载链接（可以从网盘、网盘上的压缩包内、分享链接中获取）
        :param bytes_range: 索引范围，语法符合 [HTTP Range Requests](https://developer.mozilla.org/en-US/docs/Web/HTTP/Range_requests)
        :param headers: 请求头
        :param async_: 是否异步
        :param request_kwargs: 其它请求参数
        """
        if headers:
            headers = {**headers, "Accept-Encoding": "identity", "Range": f"bytes={bytes_range}"}
        else:
            headers = {"Accept-Encoding": "identity", "Range": f"bytes={bytes_range}"}
        request_kwargs["headers"] = headers
        request_kwargs.setdefault("parse", False)
        return self.request(url, async_=async_, **request_kwargs)

    @overload
    def read_block(
        self, 
        /, 
        url: str, 
        size: int = 0, 
        offset: int = 0, 
        *, 
        async_: Literal[False] = False, 
        **request_kwargs, 
    ) -> bytes:
        ...
    @overload
    def read_block(
        self, 
        /, 
        url: str, 
        size: int = 0, 
        offset: int = 0, 
        *, 
        async_: Literal[True], 
        **request_kwargs, 
    ) -> Awaitable[bytes]:
        ...
    def read_block(
        self, 
        /, 
        url: str, 
        size: int = 0, 
        offset: int = 0, 
        *, 
        async_: Literal[False, True] = False, 
        **request_kwargs, 
    ) -> bytes | Awaitable[bytes]:
        """读取文件一定索引范围的数据
        :param url: 115 文件的下载链接（可以从网盘、网盘上的压缩包内、分享链接中获取）
        :param size: 下载字节数（最多下载这么多字节，如果遇到 EOF，就可能较小）
        :param offset: 偏移索引，从 0 开始，可以为负数（从文件尾部开始）
        :param async_: 是否异步
        :param request_kwargs: 其它请求参数
        """
        if size <= 0:
            if async_:
                async def request():
                    yield b""
                return request()
            else:
                return b""
        return self.read_bytes(
            url, 
            start=offset, 
            stop=offset+size, 
            async_=async_, 
            **request_kwargs, 
        )

    @cached_property
    def fs(self, /) -> AlistFileSystem:
        return AlistFileSystem(self)

    @cached_property
    def copy_tasklist(self, /) -> AlistCopyTaskList:
        return AlistCopyTaskList(self)

    @cached_property
    def offline_download_tasklist(self, /) -> AlistOfflineDownloadTaskList:
        return AlistOfflineDownloadTaskList(self)

    @cached_property
    def offline_download_transfer_tasklist(self, /) -> AlistOfflineDownloadTransferTaskList:
        return AlistOfflineDownloadTransferTaskList(self)

    @cached_property
    def upload_tasklist(self, /) -> AlistUploadTaskList:
        return AlistUploadTaskList(self)

    @cached_property
    def aria2_down_tasklist(self, /) -> AlistAria2DownTaskList:
        return AlistAria2DownTaskList(self)

    @cached_property
    def aria2_transfer_tasklist(self, /) -> AlistAria2TransferTaskList:
        return AlistAria2TransferTaskList(self)

    @cached_property
    def qbit_down_tasklist(self, /) -> AlistQbitDownTaskList:
        return AlistQbitDownTaskList(self)

    @cached_property
    def qbit_transfer_tasklist(self, /) -> AlistQbitTransferTaskList:
        return AlistQbitTransferTaskList(self)
