# 원격 Allegro 워크스테이션 프로토콜

이 프로토콜은 편집 PC에서 만든 SPD 작업을 같은 LAN의 라이선스 워크스테이션으로 보내고, 워크스테이션의 Allegro에서 생성한 결과를 다시 받기 위한 HTTPS API입니다. 공개 예제에서는 문서 전용 주소 `192.0.2.16`을 사용합니다. 실제 워크스테이션 주소는 앱 설정에서 지정하며, 기본 포트는 `8765`입니다.

## 보안 모델

- Agent는 최초 실행 시 자체 서명 TLS 인증서와 256비트 임의 토큰을 데이터 디렉터리에 생성합니다.
- 토큰은 `token.txt`, 인증서는 `agent-cert.pem`, 개인 키는 `agent-key.pem`에 영속 저장됩니다.
- LAN 접속에서 `RemoteClient`는 인증서 SHA-256 fingerprint를 반드시 받아야 합니다. 인증 기관 검증 대신 최초 페어링 때 전달받은 fingerprint를 고정하여 중간자 인증서를 거부합니다.
- 모든 요청은 `Authorization: Bearer <token>`을 사용하며 Agent는 `hmac.compare_digest`로 비교합니다.
- `allowed_clients`를 설정하면 지정 IP 또는 CIDR에서 온 연결만 허용합니다.
- 업로드 슬롯은 `spd`, `brd`뿐입니다. 서버 파일명은 각각 `input.spd`, `base.brd`로 고정됩니다. 클라이언트 경로, 파일명, 임의 명령은 서버 실행 인자로 사용할 수 없습니다. PID 모드는 검증한 Allegro 프로세스에 서버가 생성한 SKILL의 `load` 명령만 전달합니다.
- UUID 작업 ID를 엄격히 검사하고 URL 디코딩 후 경로를 판별하므로 `..`, 인코딩된 traversal, 임의 절대 경로를 허용하지 않습니다.
- 업로드는 압축하지 않은 raw stream이며 기본 한도는 파일당 10 GiB입니다. `AgentConfig.max_upload_bytes`로 낮추거나 높일 수 있습니다.
- 결과 ZIP에는 업로드한 입력 SPD/BRD가 포함되지 않습니다. PID 모드에서는 현재 설계의 변경 전 백업 `session-before.brd`가 포함됩니다.
- 같은 데이터 디렉터리는 OS 파일 잠금으로 한 Agent 프로세스만 사용할 수 있습니다. 두 Agent를 운영할 때는 서로 다른 데이터 디렉터리를 지정해야 합니다.
- 연결별 30초 socket timeout을 적용하고 TLS handshake를 요청 처리 스레드에서 수행하므로, handshake를 끝내지 않는 연결이 다른 클라이언트의 접속을 막지 않습니다.

Agent의 `start()` 결과에는 `host`, 실제 `port`, `token`, `token_file`, `fingerprint`, `protocol`이 들어갑니다. 운영 로그나 화면에는 토큰 자체 대신 `token_file` 경로를 표시하고, 페어링할 때만 안전한 방법으로 토큰과 fingerprint를 클라이언트에 전달합니다.

## Python API

워크스테이션에서 Agent를 시작합니다.

```python
from pathlib import Path
from brd_spd.remote import AgentConfig, WorkstationAgent

agent = WorkstationAgent(AgentConfig(
    datadir=Path(r"D:\BRD-SPD-Agent"),
    host="0.0.0.0",
    port=8765,
    allegro_exe=Path(r"C:\Cadence\SPB_24.1\tools\bin\allegro.exe"),
    allowed_clients=["192.0.2.0/24"],
))
pairing = agent.start()
```

편집 PC에서 작업을 생성하고 실행합니다.

```python
from pathlib import Path
from brd_spd.remote import RemoteClient

client = RemoteClient(
    "192.0.2.16",
    port=8765,
    token="페어링 토큰",
    fingerprint="인증서 SHA-256 fingerprint",
)

job = client.create_job({
    "layers": ["L09(MAIN_POWER1)"],
    "nets": ["MAIN_POWER1"],
    "update_components": True,
})
client.upload_file(job["id"], "spd", Path("edited.spd"), print)
client.upload_file(job["id"], "brd", Path("original.brd"), print)
client.submit_job(job["id"])

status = client.get_job(job["id"])
logs = client.get_logs(job["id"], offset=0)
result_path = client.download_result(job["id"], Path("native-result.zip"), print)
```

업로드와 다운로드의 `progress` 콜백은 사람이 읽을 수 있는 문자열 하나를 받습니다. 앱을 재시작하거나 연결을 다시 맺은 뒤에는 `client.list_jobs()`로 최근 100개 작업을 조회할 수 있습니다.

## HTTP API

| 메서드 | 경로 | 동작 |
|---|---|---|
| `GET` | `/v1/health` | 프로토콜 버전, 단일 runner, 최대 업로드 크기 확인 |
| `GET` | `/v1/allegro/{pid}` | 워크스테이션 Allegro PID, 실행 경로, 생성 시각 확인 |
| `GET` | `/v1/jobs?limit=100` | 최근 작업 조회, 최대 100개 |
| `POST` | `/v1/jobs` | `{"options": {...}}`로 작업 생성 |
| `PUT` | `/v1/jobs/{uuid}/files/spd` | raw SPD 업로드 |
| `PUT` | `/v1/jobs/{uuid}/files/brd` | raw 기준 BRD 업로드 |
| `POST` | `/v1/jobs/{uuid}/submit` | 필요한 입력 업로드 완료 후 단일 실행 큐에 제출 (PID 모드: SPD만) |
| `GET` | `/v1/jobs/{uuid}` | 영속 작업 상태 조회 |
| `GET` | `/v1/jobs/{uuid}/logs?offset=N` | 최대 1 MiB 로그와 다음 byte offset 조회 |
| `POST` | `/v1/jobs/{uuid}/cancel` | 대기 작업 취소, 새 실행의 소유 프로세스 종료, 또는 기존 세션의 협조적 취소 |
| `GET` | `/v1/jobs/{uuid}/result` | 종료 작업의 결과/진단 ZIP 다운로드 |

메타데이터 요청은 64 KiB로 제한됩니다. 지원 옵션은 `layers: list[str]`, `nets: list[str]`, `update_components: bool`, `allegro_pid: int`, `allegro_creation_time: int`입니다. 알 수 없는 옵션은 거부합니다. PID는 1~4,294,967,295이며 생성 시각은 `check_pid`가 반환한 Windows FILETIME 값입니다. 생성 시각은 PID와 함께 지정하며 확인 후 PID가 재사용되면 거부됩니다. Agent는 작업 생성 시 실제 실행 경로와 생성 시각을 저장하고 제출·전달 시 재검사합니다. `/v1/health`의 `capabilities`에 `allegro_pid`가 있으면 이 기능을 지원합니다.

상태 흐름은 다음과 같습니다.

```text
created -> queued -> running -> succeeded
                    |       -> failed
                    |       -> cancelled
                    `restart-> interrupted

running -> cancel_requested -> cancelled
                           `-> succeeded
```

취소 요청은 `cancel_requested`로 먼저 기록합니다. `created`나 `queued` 작업은 곧바로 `cancelled`가 되고, 실행 중인 작업은 취소를 확인한 뒤 `cancelled`로, 이미 저장을 마친 너무 늦은 취소는 `succeeded`, `cancellation_too_late=true`로 남습니다.

Agent는 동시에 하나의 native 작업만 실행합니다. 실행 중 Agent가 재시작되면 새 프로세스로 시작한 `running` 또는 `cancel_requested` 작업은 `interrupted`로 바꿉니다. 이미 명령을 전달한 PID 작업은 결과 표식 관찰을 재개하며 명령을 다시 보내지 않습니다. `queued` 작업은 다시 큐에 넣습니다.

종료 상태는 결과 ZIP이 임시 파일에서 원자적으로 배치된 다음 공개됩니다. 따라서 `succeeded`, `failed`, `cancelled`, `interrupted`로 조회된 작업은 즉시 결과 또는 진단 ZIP을 받을 수 있습니다. ZIP 안의 `job.json`도 같은 최종 상태를 기록합니다.

## Native 실행 계약

Agent는 다음 고정 흐름만 실행합니다.

1. `generate_bundle(input.spd, bundle, layers=..., nets=..., update_components=..., progress=...)`
2. 업로드한 `base.brd`를 작업 전용 `bundle/base.brd`로 복사
3. `cwd=bundle`에서 다음 고정 인자 실행

```text
C:\Cadence\SPB_24.1\tools\bin\allegro.exe -nograph -s run.scr base.brd
```

설치된 `share/pcb/batchhelp/allegro.txt`는 `-s <script>`, `-nograph`, 마지막 board database 인자를 공식 지원합니다. Agent는 shell을 사용하지 않으며 업로드된 스크립트나 사용자 지정 명령을 실행하지 않습니다. `run.scr`과 `design.il`은 로컬 `generate_bundle` 구현이 생성합니다.

성공은 다음 조건을 모두 만족해야 합니다.

- Allegro 프로세스 반환 코드가 0
- `result.json`의 `status`가 `success`
- `result.brd`가 존재하고 크기가 0보다 큼

Native 결과 계약은 다음과 같습니다.

```json
{
  "status": "success",
  "result_brd": "result.brd",
  "execution_log": "execution.log",
  "planes_replaced": 1,
  "components_updated": 12
}
```

실패는 `{"status":"failed","error":"...","execution_log":"execution.log"}`이며 BRD 성공을 주장하지 않습니다.

성공 ZIP에는 검증된 `result.brd`, `execution.log`, `generation.log`, `generation.report.json`, `result.json`, `manifest.json`, `agent-runner.log`, `job.json` 중 실제 생성된 파일이 들어갑니다. 실패·취소·중단 작업도 같은 endpoint에서 진단 ZIP을 받을 수 있지만 `result.brd`는 포함하지 않습니다.

취소 시 Agent가 시작하고 추적 중인 프로세스만 종료합니다. Windows에서는 그 PID의 프로세스 트리를 `taskkill /PID <owned-pid> /T /F`로 종료하여 Allegro launcher의 자식 프로세스가 남지 않게 합니다. 번들 생성 중에는 parser 진행 콜백에서 취소를 확인하며, native 실행 직전에도 다시 확인합니다.

### 기존 Allegro PID 모드

`check_pid(pid)` 결과의 `creation_time`을 `allegro_creation_time` 옵션으로 전달하고 `allegro_pid`를 지정합니다. 이 모드에서는 BRD 업로드를 거부하고, `generate_bundle(..., session_mode=True)`를 호출합니다. 생성된 절대 경로의 `design.il`을 해당 PID 소유의 단일 Allegro 창에 `WM_COPYDATA`로 전달합니다. 명령, 경로, 스크립트 자체를 클라이언트에서 지정하는 API는 없습니다.

`execution.started`는 진입 표식, `finished.flag`는 `result.json`을 닫은 이후 작성하는 완료 표식입니다. PID 모드 성공은 완료 표식, `status=success`, 0보다 큰 `result.brd`를 모두 요구하며 Allegro 종료 코드는 요구하지 않습니다. 대상 세션은 계속 실행됩니다. `design_modified`는 커밋 여부를, `recovery_brd`는 변경 전 백업 경로를 기록합니다. 커밋 후 저장 실패는 실패로 보고하되 변경된 설계를 자동으로 되돌리지 않습니다.

취소는 `cancel.flag`로 요청합니다. SKILL은 시작 전과 변경 단위 사이, 커밋 전에 확인하고 가능한 트랜잭션을 롤백합니다. 기존 PID를 강제 종료하지 않습니다. 너무 늦은 취소 뒤 실제 저장이 성공하면 작업은 `succeeded`, `cancellation_too_late=true`로 남습니다. 메시지 전달 시간 초과는 완료 표식을 기다리며 자동 재전송하지 않습니다. 명령 진입을 60초 이내에 확인하지 못하면 취소 표식을 남겨 늦게 진입한 작업도 변경 전에 중단하게 합니다.

진입 후 관찰 한도는 `AgentConfig.session_timeout_seconds`(기본 21,600초), 취소 확인 대기는 10초입니다. 한도 초과 또는 dispatch 이후 관찰 오류는 `interrupted`, `native_pending=true`, `design_modified=null`로 기록하여 native 실패와 구분합니다. 이후 상태·작업 목록·결과 조회에서 `finished.flag`가 발견되면 실제 native 결과로 상태와 ZIP을 갱신합니다. 이 경우에만 종료 상태가 나중에 변경될 수 있습니다. 관찰 재개나 결과 재확인은 명령을 다시 보내지 않습니다. 미완료 snapshot은 진단 ZIP에서 제외합니다. 최종 성공 판정은 결과 BRD 외에도 nonempty 백업, `design_modified=true`, 예상한 `recovery_brd` 경로를 요구합니다.
