# 개인 수정본 (fork)

원본 ripe-avocado/navien_smart_ha v0.16.0 을 기반으로 NR-65D 보일러 지원을 직접 추가·수정한 버전입니다.
원본 업데이트로 덮어쓰이지 않도록 포크해서 HACS 에 이 저장소를 등록해 씁니다.

- 원본 v0.24.0 은 `upstream-v0.24.0` 브랜치에 그대로 보관
- 보일러 엔티티 unique_id(`<deviceId>_climate`, `_power`, `_water_heater` 등)는 원본 v0.22+ 와 다르므로 원본으로 바꾸면 엔티티가 새로 생김
- 주요 패치: pwchg_late.py(비밀번호 변경 요구 건너뛰기), coordinator.py 범위 대체값, boiler.py NR-65D feature 대체값, climate.py 온도 입력 묶음(1.5초)
- manifest version 0.16.1 = HA 에서 쓰던 0.16.0 수정본과 코드 동일 (버전 표기만 올림)
- hacs.json 의 zip_release 제거 (포크에는 릴리스 zip 이 없어 기본 브랜치에서 설치)
