"""전원 스위치.

`functions.powerCtrl` 이 없는 모델에는 만들지 않는다.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from homeassistant.exceptions import HomeAssistantError

from . import NavienSmartConfigEntry
from .airone import AironeDevice
from .boiler import BoilerDevice
from .const import MODE_HEAT, MODE_POWER_OFF
from .coordinator import NavienSmartCoordinator
from .entity import AironeEntity, BoilerEntity, NavienSmartEntity
from .models import NavienDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        NavienSmartPowerSwitch(coordinator, device)
        for device in (coordinator.data or {}).values()
        if device.has_power_ctrl
    ]
    # **서버가 잠금 기능을 알려줄 때만 만든다.** 앱은 모델 번호 표로 가르는데
    # (`MateInfoData` 의 `case 257: supportLock = false`) 표에 실린 모델이 다섯
    # 개뿐이라 새 모델을 못 따라간다. 서버 쪽 선언을 쓴다.
    entities.extend(
        NavienSmartChildLockSwitch(coordinator, device)
        for device in (coordinator.data or {}).values()
        if device.has_lock_mode
    )
    entities.extend(
        AironePowerSwitch(coordinator, device) for device in coordinator.airone.values()
    )
    # 보일러 전원. **외출은 여기 없다** — 외출·실내난방·온돌난방이 겹쳐 동작할 수
    # 없으므로 `climate` 의 preset 으로 묶었다. 스위치로도 두면 같은 것을 두 곳에서
    # 조작하게 되어 상태가 어긋나 보인다.
    entities.extend(
        BoilerPowerSwitch(coordinator, boiler)
        for boiler in coordinator.boiler.values()
        if boiler.supports("powerUse")
    )
    async_add_entities(entities)


def _raise_if_failed(outcome: dict[str, Any], prefix: str) -> None:
    """보내지 못한 기기가 있으면 이유를 담아 올린다.

    조용히 삼키면 사용자는 「눌렀는데 아무 일도 안 난다」만 겪는다.
    """
    reasons = [
        str(item.get("reason"))
        for item in outcome.get("results") or []
        if not item.get("sent")
    ]
    if reasons:
        raise HomeAssistantError(f"{prefix}: {'; '.join(reasons)}")


class NavienSmartPowerSwitch(NavienSmartEntity, SwitchEntity):
    """`operationMode` 0/1 로 전원을 끄고 켠다."""

    # **기기의 대표 엔티티다.** 이름을 두지 않으면 HA 가 기기 이름을 그대로 쓴다
    # (`Entity.use_device_name` — "the single main feature of a device").
    #
    # 그래서 목록에서 **항상 맨 위**에 온다. 기기 페이지는 표시 이름을 사전순으로
    # 정렬하는데, 한글은 `우(ㅇ)` 가 `좌(ㅈ)` 보다 앞이라 좌우 분리형에서
    # `우측 → 전원 → 좌측` 이라는 이상한 순서가 나왔다.
    #
    # **자격이 있는가** — 전원은 어느 모델이든 기기당 하나다. `operationMode` 가
    # `heater` 바깥에 있고, 좌/우를 아무리 만져도 그 값은 안 움직인다(실측).
    # 좌우 난방 단계는 둘이라 이름을 유지한다. `tplink` 가 쓰는 기준과 같다 —
    # 「기기당 하나면 기기 이름, 여럿이면 자기 이름」.
    #
    # `_attr_name` 이 `translation_key` 를 이긴다 (`Entity._name_internal` 첫 줄).
    # 번역 항목은 에어원 전원이 계속 쓰므로 남겨둔다.
    _attr_name = None
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_power"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or device.operation_mode is None:
            return None
        return device.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        return {"operation_mode": device.operation_mode, "mode": device.mode_name}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_mode(MODE_HEAT)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_mode(MODE_POWER_OFF)

    async def _async_set_mode(self, mode: int) -> None:
        device = self.device
        if device is None:
            return
        await self.coordinator.async_send(device, {"operationMode": mode})


class NavienSmartChildLockSwitch(NavienSmartEntity, SwitchEntity):
    """조작 잠금. 기기 본체 버튼을 잠근다.

    **v0.12.0 은 이것을 읽기 전용 센서로 만들었다. 틀렸다.** 앱에 자물쇠 버튼이
    있는데 못 찾았다 — 명령을 문자열 그대로 넘기는 호출만 훑었고, 잠금은
    `"lock-on"`/`"lock-off"` 를 **변수로** 넘긴다.

    켜면 잠근다. 앱 버튼과 같은 방향이다.
    """

    _attr_icon = "mdi:lock"

    def __init__(self, coordinator: NavienSmartCoordinator, device: NavienDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_child_lock"
        self._attr_name = "조작 잠금"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        return device.child_lock if device is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, locked: bool) -> None:
        device = self.device
        if device is None:
            return
        await self.coordinator.async_send(
            device, device.build_child_lock_desired(locked)
        )


class AironePowerSwitch(AironeEntity, SwitchEntity):
    """`running` 1/2 로 전원을 끄고 켠다.

    구세대는 이 값이 반대다(운전=2). `coordinator` 가 구세대를 걸러내므로 여기서는
    신형 규약만 다룬다 — 세대 판정을 두 곳에 두면 한쪽만 고치는 실수가 난다.
    """

    # 매트 전원과 같은 이유로 기기 대표다 (위 `NavienSmartPowerSwitch` 주석).
    # 에어원도 전원은 기기당 하나이고, 운전모드·풍량·희망습도는 여럿이라
    # 각자 이름을 유지한다.
    _attr_name = None
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: NavienSmartCoordinator, device: AironeDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_power"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or device.running is None:
            return None
        return device.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        return {"running": device.running, "state": device.running_name}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_power(False)

    async def _async_power(self, turn_on: bool) -> None:
        device = self.device
        if device is None:
            return
        await self.coordinator.async_airone_power(device, turn_on)


class BoilerPowerSwitch(BoilerEntity, SwitchEntity):
    """보일러 전원.

    **꺼짐(`operationMode` 1)만 꺼진 것으로 본다.** 온수전용(외출, 4)은 난방을
    쉬는 것이고 전원은 켜져 있다 — 그것은 `climate` 의 preset 으로 고른다.

    켤 때 `power-on` 을 보내면 기기가 **마지막에 쓰던 모드로** 돌아간다
    (실측: 꺼짐에서 켜면 온수전용으로 왔다). 우리가 모드를 고르지 않는다.
    """

    _attr_name = "전원"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: NavienSmartCoordinator, device: BoilerDevice) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_power"

    @property
    def is_on(self) -> bool | None:
        device = self.device
        if device is None or not device.has_status:
            return None
        mode = device.operation_mode
        return None if mode is None else mode != 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        outcome = await self.coordinator.async_boiler_request("power_on")
        _raise_if_failed(outcome, "보일러를 켜지 못했습니다")

    async def async_turn_off(self, **kwargs: Any) -> None:
        outcome = await self.coordinator.async_boiler_request("power_off")
        _raise_if_failed(outcome, "보일러를 끄지 못했습니다")
