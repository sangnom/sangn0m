# hermes-profile-audit

Hermes Agent(NousResearch/hermes-agent)에서 **특정 프로필만 동작하고 나머지 프로필은
아무 반응이 없는** 증상을 진단하는 읽기 전용 스크립트.

```bash
python3 tools/hermes-profile-audit.py            # ~/.hermes 를 검사
python3 tools/hermes-profile-audit.py --json     # 기계 판독용 출력
python3 tools/hermes-profile-audit.py --root /opt/data   # Docker 등 커스텀 루트
```

비밀값은 절대 출력하지 않는다. 키 **이름**, 값의 유무, 그리고 SHA-256 앞 8자리
지문만 찍는다. 지문이 같으면 두 프로필이 같은 자격증명을 쓰고 있다는 뜻이다.

## Hermes의 "계정연결"이 저장되는 곳

프로필마다 완전히 분리된 세 곳에서 독립적으로 해석된다.

| 위치 | 담는 것 | 다른 프로필/전역으로의 폴백 |
|---|---|---|
| `<profile>/.env` | 프로바이더 API 키, 플랫폼 봇 토큰 | **없음** (멀티플렉스 ON일 때). 루트 `~/.hermes/.env`도, 셸 export도 안 보임 |
| `<profile>/auth.json` → `providers.<id>` | OAuth / device-code 자격증명 풀 | **있음** — 프로필에 없으면 루트 `~/.hermes/auth.json`의 같은 프로바이더 항목을 읽음 |
| `<profile>/auth.json` → `active_provider` | 로그인된 기본 프로바이더 | **없음** — 프로필 파일만 본다 |
| `<profile>/config.yaml` → `model.provider` / `model.default` | 사용할 모델·프로바이더 | 없음 (프로필별 파일) |

근거:
- `agent/secret_scope.py::build_profile_secret_scope` — 스코프를 `<home>/.env`
  하나에서만 만든다. `get_secret()`은 멀티플렉스가 켜져 있으면 스코프를 최종
  권위로 취급하고 `os.environ`으로 내려가지 않는다.
- `hermes_cli/auth.py::_load_provider_state` — 프로바이더별 전역 폴백 있음.
- `hermes_cli/auth.py::get_active_provider` — `_load_auth_store()`(프로필 전용)만
  읽으므로 전역 폴백 없음.
- `gateway/run.py::_profile_runtime_scope` — 인바운드 턴마다 위 스코프를 설치.

## 이 증상이 생기는 가장 흔한 경로

`hermes profile create <name>` 을 `--clone` 없이 실행하면
(`hermes_cli/profiles.py::create_profile`) 주석만 들어 있는 **빈 `.env`** 가
생성된다. 그래서:

- `hermes profile show <name>` 은 `.env: exists` 라고 표시한다 — 자격증명이
  0개여도 똑같이 표시된다.
- `gateway.multiplex_profiles: true` 이면 그 프로필의 시크릿 스코프가 최종
  권위이므로, 셸 환경변수와 루트 `.env` 를 못 본다 → 조용히 아무것도 안 함.
- 플랫폼 어댑터는 토큰이 없으면 `_platform_has_bot_credential()` 에서 걸러져
  `logger.info` 한 줄만 남기고 **건너뛴다**. 에러가 안 뜨는 이유.

## 고치는 법

```bash
# 1) 동작하는 프로필의 자격증명을 그대로 복사
cp ~/.hermes/profiles/<working>/.env ~/.hermes/profiles/<broken>/.env
chmod 600 ~/.hermes/profiles/<broken>/.env

# 2) 또는 처음부터 복제해서 생성 (config.yaml + .env + SOUL.md + skills)
hermes profile create <name> --clone-from <working>

# 3) 프로필별로 계정을 따로 붙이고 싶다면
hermes -p <name> auth login <provider>
hermes -p <name> model
```

멀티플렉스 게이트웨이를 쓰는 경우 allowlist도 확인한다. 값이 설정돼 있으면
거기에 없는 프로필은 아예 서빙되지 않는다
(`hermes_cli/profiles.py::profiles_to_serve`).

```bash
hermes config get gateway.multiplex_profiles
hermes config get gateway.multiplex_profile_allowlist
```
