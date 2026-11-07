"""나비엔 스마트 통합.

나비엔 스마트 앱이 쓰는 서버에 직접 붙는다. 공식 API 가 아니다.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers import config_validation as cv

from .api import NavienSmartApi, NavienSmartAuthError, NavienSmartError
from . import pwchg_late  # noqa: F401  비밀번호 변경 요구 자동 건너뛰기 패치(2026-10-21)
from .const import CONF_HOME_SEQ, DOMAIN
from .coordinator import NavienSmartCoordinator

_LOGGER = logging.getLogger(__name__)

# 보일러 상태 조회 시험용. **봉투가 확정되면 없어질 서비스다** — 그래서 이름에
# 목적을 적었다. 값을 싣지 않는 조회라 설정을 바꾸지 않는다.
SERVICE_BOILER_REQUEST = "boiler_request"
_BOILER_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Optional("action", default="status"): cv.string,
        vol.Optional("value"): vol.Coerce(float),
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    # 온도형(`0.5C`)은 climate, 단계형(`1.0L`)은 select 로 갈린다. 둘 다 등록하고
    # 각 플랫폼이 자기 축의 기기만 골라 간다.
    Platform.CLIMATE,
    # 에어원 목표 습도만 쓴다. 매트 단계는 `select` 다 — 단계는 연속량이 아니다.
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    # 보일러 온수만 쓴다. 봉투·명령 코드를 실기기로 확인한 뒤 열었다.
    Platform.WATER_HEATER,
]

NavienSmartConfigEntry = ConfigEntry[NavienSmartCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NavienSmartConfigEntry) -> bool:
    """자격증명으로 로그인하고 코디네이터를 세운다."""
    # 폼 로그인이 쿠키를 물어야 해서 전용 세션을 쓴다. 공용 세션을 오염시키지 않는다.
    http = async_create_clientsession(hass)
    api = NavienSmartApi(http, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])

    try:
        session = await api.async_login()
    except NavienSmartAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except NavienSmartError as err:
        raise ConfigEntryNotReady(str(err)) from err

    home_seq = entry.data.get(CONF_HOME_SEQ) or session.homes[0]["homeSeq"]

    coordinator = NavienSmartCoordinator(hass, entry, api, int(home_seq))
    await coordinator.async_config_entry_first_refresh()
    # 구세대는 상태 요청에 답하지 않는다. 마지막으로 알던 값을 먼저 채운다.
    await coordinator.async_restore_state()

    if not coordinator.data and not coordinator.airone and not coordinator.boiler:
        _LOGGER.warning(
            "home %s 에서 지원 가능한 기기를 찾지 못했습니다. "
            "건너뛴 기기가 있으면 위 경고를 확인해 주세요.",
            home_seq,
        )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 엔티티가 준비된 뒤 구독을 시작한다.
    await coordinator.async_start_mqtt()
    entry.async_on_unload(coordinator.async_stop_mqtt)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    if coordinator.boiler:
        _async_register_boiler_service(hass)
    return True


def _async_register_boiler_service(hass: HomeAssistant) -> None:
    """보일러 조회·제어 서비스를 등록한다.

    봉투는 앱을 디컴파일해 확정한 것이지만 **실기기로 아직 검증하지 않았다.**
    그래서 엔티티를 만들지 않고 서비스로만 열어 둔다 — 사용자가 의도적으로
    부를 때만 동작한다. 조회(`action: status`)는 값을 싣지 않아 안전하다.
    """
    if hass.services.has_service(DOMAIN, SERVICE_BOILER_REQUEST):
        return

    async def _handle(call: ServiceCall) -> ServiceResponse:
        results: list[dict] = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator: NavienSmartCoordinator | None = getattr(
                entry, "runtime_data", None
            )
            if coordinator is None or not coordinator.boiler:
                continue
            outcome = await coordinator.async_boiler_request(
                action=call.data["action"],
                value=call.data.get("value"),
                dry_run=call.data["dry_run"],
            )
            results.extend(outcome["results"])
        return {"results": results}

    hass.services.async_register(
        DOMAIN,
        SERVICE_BOILER_REQUEST,
        _handle,
        schema=_BOILER_REQUEST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_unload_entry(hass: HomeAssistant, entry: NavienSmartConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: NavienSmartConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
