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

## OAuth 계정을 프로필별로 나눠 붙인 경우 — 별도의 실패 경로

Hermes는 Nous OAuth 토큰을 **설치 전체에 하나뿐인 공유 슬롯**에 보관한다.

    <hermes-root>/shared/nous_auth.json

이 파일에는 **계정 식별자가 없다.** 그리고 Nous 토큰을 해석하는 모든 경로가
매번 이 공유 저장소를 프로필의 자기 토큰 위에 덮어쓴다
(`hermes_cli/auth.py:6147`, `:6587`, `:6609` → `_merge_shared_nous_oauth_state`).
병합 조건은 "공유 refresh_token이 내 것과 다르거나, 공유 access_token이 더
늦게 만료된다"뿐이고 **어느 계정인지는 비교하지 않는다.** 리프레시에 성공하면
반대로 자기 토큰을 공유 저장소에 다시 써 넣는다(`:6225`, `:6569`).

설계 의도는 주석에 그대로 적혀 있다 — "새 프로필이 device-code 플로우를 다시
돌리지 않고 원탭으로 import 하게" (`auth.py:5468`). 즉 **설치당 Nous 계정 1개**를
전제한 기능이다.

프로필마다 **다른** 계정을 붙이면 이렇게 무너진다.

1. 프로필 A가 토큰을 갱신 → 공유 저장소가 A의 토큰으로 바뀐다.
2. 프로필 B가 다음 갱신 시 공유 저장소를 자기 상태에 병합 → B는 A의 계정이 된다.
3. refresh_token은 **1회용**이다. B가 A의 토큰을 다시 쓰면 포털이
   `refresh_token_reused` / `invalid_grant`로 거절하고, 이는 터미널 오류라
   `relogin_required`가 붙는다(`auth.py:5709`).
4. `_clear_shared_nous_state()`가 공유 저장소를 지운다 → 남은 프로필들도 연쇄로 죽는다.

결과: **마지막에 갱신한 프로필 하나만 살아남고 나머지는 전부 죽는다.**

`--json` 없이 스크립트를 돌리면 각 프로필의 access token을 로컬에서 디코드해
(네트워크 없음, 토큰 자체는 출력하지 않음) 어느 계정에 묶여 있는지 보여주고,
같은 refresh_token 지문을 공유하는 프로필들을 collision으로 잡아낸다.

### 계정을 실제로 분리하려면

`_nous_shared_auth_dir()`는 `HERMES_SHARED_AUTH_DIR` 환경변수를 존중한다
(`auth.py:5503`). 원래 테스트용 노브지만 `os.getenv`로 직접 읽으므로 런타임에도
동작한다. 프로필마다 다른 값을 주면 공유 슬롯이 분리된다.

```bash
export HERMES_SHARED_AUTH_DIR=~/.hermes/profiles/coder3/shared
hermes -p coder3 auth login nous      # 이 프로필 전용 토큰 체인 발급
```

주의:
- 그 프로필의 **모든** 프로세스(게이트웨이, TUI, cron)에 같은 값이 필요하다.
  런처 스크립트나 서비스 유닛의 `Environment=`에 넣어야 한다.
- `HERMES_SHARED_AUTH_DIR`는 `_GLOBAL_ENV_EXACT` 목록에 없어서
  멀티플렉스 시크릿 스코프의 대상이 아니다 → 프로필 `.env`에 넣어도
  멀티플렉스 게이트웨이에서는 `os.environ`에 실리지 않는다. 프로세스 환경변수로
  줘야 한다.
- 이미 오염된 프로필은 값을 분리한 뒤 **재로그인**해야 한다. 기존 토큰 체인은
  이미 다른 계정 것이거나 소모된 상태다.

계정을 완전히 갈라야 한다면 `HERMES_HOME` 자체를 루트째 분리하는 쪽이 확실하다.

### 같은 문제가 있는 다른 프로바이더

프로필이 아니라 HOME/설치 전역에 자격증명을 두는 경로들:

| 프로바이더 | 경로 | 범위 |
|---|---|---|
| Nous OAuth | `<root>/shared/nous_auth.json` | 설치 전역 (위 참조) |
| Qwen OAuth | `~/.qwen/oauth_creds.json` (`auth.py:2785`) | HOME 전역 |
| Anthropic | `~/.claude/.credentials.json` 자동 탐지 | HOME 전역 |

`auth.json`의 프로바이더 항목도 프로필에 없으면 루트 `~/.hermes/auth.json`으로
폴백한다(`auth.py:1533`). 그래서 "프로필에 계정을 안 붙였는데 왜 되지?" 또는
"루트 계정 토큰이 왜 갑자기 죽었지?"가 같이 생긴다 — 명명 프로필이 루트의
토큰을 회전시키고 그 결과를 루트 스토어에 다시 쓰기 때문이다
(`_save_provider_state_to_source`).

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
