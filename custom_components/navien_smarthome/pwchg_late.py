"""비밀번호 변경 요구 자동 건너뛰기 패치 (2026-10-21).

서버가 로그인 때 비밀번호 변경 화면(`passwordChg`)을 띄우면 `api.py` 는 오류만 내고
멈춘다. 그러면 연동 전체가 인증 실패로 `unavailable` 이 된다.

이 모듈은 나비엔 앱의 "다음에 변경하기"와 같은 요청(`POST {LOGIN_URL}/pwchgLate`)을
보낸 뒤 **딱 한 번만** 다시 로그인한다. 같은 방식을 homebridge-navien-smart 의
`navien.auth.ts` 가 쓴다.

`api.py` 를 직접 고치지 않고 `NavienSmartApi._async_form_login` 을 감싼다.
되돌리려면 `__init__.py` 의 `pwchg_late` import 한 줄과 이 파일을 지우고 HA 를
재시작하면 된다.
"""

from __future__ import annotations

import logging

import aiohttp

from .api import NavienSmartApi, NavienSmartAuthError
from .const import LOGIN_URL, REQUEST_TIMEOUT_SECONDS, USER_AGENT

_LOGGER = logging.getLogger(__name__)

# api.py 가 비밀번호 변경 요구일 때 내는 메시지의 일부. 이 경우에만 개입한다.
_PWCHG_MESSAGE = "비밀번호 변경을 요구"

_original_form_login = NavienSmartApi._async_form_login


async def _async_pwchg_late(api: NavienSmartApi) -> None:
    """'다음에 변경하기' 요청. 같은 쿠키 세션으로 보내야 하므로 api 의 세션을 쓴다."""
    async with api._http.post(
        f"{LOGIN_URL}/pwchgLate",
        headers={
            "User-Agent": USER_AGENT,
            "Origin": LOGIN_URL,
            "Referer": f"{LOGIN_URL}/member/login",
            "Content-Type": "application/json",
        },
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
    ) as resp:
        body = await resp.text()
    _LOGGER.debug("pwchgLate 응답: status=%s body=%s", resp.status, body[:200])


async def _async_form_login_with_pwchg_late(self: NavienSmartApi):
    """원래 로그인을 부르고, 비밀번호 변경 요구일 때만 건너뛴 뒤 한 번 재시도한다."""
    try:
        return await _original_form_login(self)
    except NavienSmartAuthError as err:
        if _PWCHG_MESSAGE not in str(err):
            raise
        _LOGGER.warning(
            "서버가 비밀번호 변경을 요구해 '다음에 변경하기'(pwchgLate)를 요청하고 "
            "다시 로그인합니다."
        )
        try:
            await _async_pwchg_late(self)
        except (aiohttp.ClientError, TimeoutError) as late_err:
            # 건너뛰기 요청 자체가 실패하면 원래 오류를 그대로 알린다.
            _LOGGER.warning("pwchgLate 요청 실패: %s", late_err)
            raise err from late_err
        # 재시도는 한 번뿐이다. 또 변경을 요구하면 원래처럼 오류가 난다.
        return await _original_form_login(self)


_async_form_login_with_pwchg_late._pwchg_late_patched = True  # type: ignore[attr-defined]

if not getattr(NavienSmartApi._async_form_login, "_pwchg_late_patched", False):
    NavienSmartApi._async_form_login = _async_form_login_with_pwchg_late
