# BRD-SPD-IPC2581

**PowerSI SPD에서 Allegro용 SKILL 스크립트를 생성하는 Windows 프로그램입니다.**
라이선스가 있는 별도 워크스테이션의 Allegro를 노트북에서 실행하고, 진행 로그와 결과를 회수할 수 있습니다. GUI와 CLI를 제공합니다.

> **이 프로그램은 원본 Allegro BRD를 완전히 재구성하는 도구가 아닙니다.**
> SKILL 경로는 **동일 보드의 원본 BRD**를 바탕으로 SPD의 plane을 native static shape로 반영합니다. 원본의 동적 plane 생성 규칙은 SPD에서 복원할 수 없습니다. 스크립트 생성, Allegro 실행 완료, 설계 검토 완료는 서로 구분하여 기록합니다. IPC-2581 구현은 보관하지만 GUI와 CLI 변환 기능은 현재 비활성화되어 있습니다.

## 설치와 실행

1. [Releases](https://github.com/yunhyok/BRD-SPD-IPC2581/releases)의 `BRD-SPD-IPC2581-Setup-0.5.0.exe`를 노트북과 워크스테이션에 설치합니다. 설치 후 같은 `BRD-SPD-IPC2581.exe`를 두 PC에서 사용합니다. 별도 Python 설치는 필요하지 않습니다.
2. 워크스테이션에서 같은 **BRD-SPD-IPC2581.exe**를 열고 **원격 Allegro 작업** 탭의 **워크스테이션 (Allegro 실행)** 역할을 선택합니다. Allegro 실행 파일과 Agent 설정을 검토한 뒤 **Agent 시작**을 누르고, 표시된 접속 토큰과 인증서 지문을 노트북에 입력합니다. 역할 선택만으로 Agent가 시작되지는 않습니다.
3. 노트북에서 같은 앱의 **노트북 (작업 요청)** 역할을 선택하고 워크스테이션 IP를 입력한 뒤 SPD를 불러옵니다. **찾아보기…**로 파일을 선택하면 자동으로 읽으며, 직접 입력하거나 저장된 경로는 **SPD 불러오기**를 누르거나 경로 입력란에서 Enter를 누릅니다. 로딩 후 대상 레이어·NET과 부품 옵션, 실행 버튼이 활성화됩니다. 같은 파일을 다시 불러오면 이전 대상 선택을 복원하고, 이번 SPD에 없는 이름만 제외합니다.
4. **Allegro PID**를 입력하면 해당 프로세스에 현재 열린 BRD에 반영합니다. PID를 비우면 업로드한 원본 BRD로 새 Allegro를 실행합니다. 진행 로그를 확인하고 완료 후 결과 ZIP을 다운로드합니다.

PID는 워크스테이션 작업 관리자의 **세부 정보 → allegro.exe → PID**에서 확인합니다. **PID 확인** 후 실행하면 프로세스 경로와 생성 시각까지 대조하여 재사용된 PID를 거부합니다. PID 모드에서는 변경 전 상태를 `session-before.brd`에 백업하고, 완료 후 `result.brd`를 해당 Allegro 창에 열어 둡니다. 에이전트와 Allegro는 같은 Windows 로그인 세션 및 권한 수준에서 실행하세요. PID는 프로그램을 다시 열 때 자동 복원하지 않습니다.

IP는 언제든 바꿀 수 있습니다. 인증서와 토큰은 워크스테이션의 작업 폴더에 보관되므로 IP만 변경된 경우 다시 발급할 필요가 없습니다. Allegro 기본 경로는 `C:\Cadence\SPB_24.1\tools\bin\allegro.exe`입니다. 에이전트가 실행 중인 Windows 사용자에게 Allegro 라이선스와 보드 접근 권한이 있어야 합니다.

**Native SKILL 생성** 탭은 노트북 역할에서 스크립트 묶음만 생성합니다. 워크스테이션 역할에서는 이 탭이 비활성화되며, 사용하려면 노트북 역할로 전환합니다. 원격 Allegro 작업 역할은 마지막 선택을 유지합니다. 원격 작업·Native SKILL 생성·SPD 로딩·레이어/NET 목록 선택 창이 진행 중이거나 Agent가 실행 중이면 역할을 바꿀 수 없습니다. Agent가 실행 중인 경우에는 먼저 중지합니다. 기존 **BRD-SPD-IPC2581 Workstation Agent** 별도 실행 파일도 계속 호환됩니다. **Help / 도움말** 메뉴는 원하는 주제를 열고, **F1**은 현재 역할에 맞는 [오프라인 HTML 도움말](docs/help/index.html)을 프로그램 안의 내장 뷰어로 엽니다. Chrome, Edge, WebView2 또는 인터넷 설치는 필요하지 않습니다.

대상 레이어·NET의 **목록에서 선택…**을 누르면 별도 선택 창이 열립니다. **전체 선택**, **선택 해제**, **Ctrl/Shift 다중 선택**을 지원하며 NET 목록은 선택한 레이어에 맞춰 표시됩니다. 빈 입력란은 전체 대상이라는 뜻이며 목록 창에서 0개 선택한 상태로 적용할 수는 없습니다. SPD 경로를 바꾸면 기존 선택을 초기화하고 다시 불러와야 합니다. 로딩 전에도 도움말, 연결·PID 확인, 기존 작업 결과 받기는 사용할 수 있습니다.

설치파일은 코드 서명이 없는 초기 릴리스입니다. 설치 경로는 기본적으로 `%LOCALAPPDATA%\Programs\BRD-SPD-IPC2581`입니다.

## 변환 경로

```mermaid
flowchart LR
    L[노트북: SPD + PID 또는 원본 BRD] -->|인증된 HTTPS| W[워크스테이션 에이전트]
    W --> S[SKILL 생성 + 손실 보고서]
    S --> A[지정 PID 또는 새 Allegro 24.1]
    A --> R[새 result.brd + 실행 로그]
    R -->|조회·다운로드| L
```

원격 통신은 인증서 지문을 고정한 HTTPS와 접속 토큰을 사용합니다. 워크스테이션에서 허용할 노트북 IP를 선택적으로 제한할 수 있습니다. 노트북은 서버를 열지 않습니다. 작업마다 별도 디렉터리와 복사본 또는 변경 전 백업을 사용하고 에이전트는 한 번에 한 작업만 실행합니다.

## CLI

설치된 `brd-spd-ipc2581-cli.exe`를 사용하거나 소스에서 다음 명령을 실행합니다.

```powershell
python -m pip install -r requirements.txt
python launch_cli.py inspect edited.spd -o inventory.json
python launch_cli.py validate result.xml --xsd schemas/IPC-2581B1.xsd
python launch_cli.py skill edited.spd -o skill-bundle --layer TOP --net VCC
```

워크스테이션에서 에이전트를 실행하는 CLI도 제공합니다.

```powershell
python launch_cli.py serve --host 0.0.0.0 --port 8765 --allegro-exe "C:\Cadence\SPB_24.1\tools\bin\allegro.exe"
python launch_cli.py remote --host WORKSTATION_IP --token-file pairing-token.txt --fingerprint CERT_SHA256 health
python launch_cli.py remote --host WORKSTATION_IP --token-file pairing-token.txt --fingerprint CERT_SHA256 submit edited.spd --base-brd original.brd
python launch_cli.py remote --host WORKSTATION_IP --token-file pairing-token.txt --fingerprint CERT_SHA256 check-pid 12345
python launch_cli.py remote --host WORKSTATION_IP --token-file pairing-token.txt --fingerprint CERT_SHA256 submit edited.spd --allegro-pid 12345
```

`remote status`, `remote logs`, `remote cancel`, `remote download`에 반환된 작업 ID를 전달합니다. 토큰은 명령행 문자열 대신 파일 또는 `BRDSPD_TOKEN` 환경변수로 제공하여 셸 기록에 남기지 않습니다.

`convert`와 `import-brd` 명령은 현재 비활성화되어 있으며 실행 시 종료 코드 `1`과 안내 메시지를 반환합니다. `inspect`, `validate` 및 핵심 IPC 라이브러리는 보관되어 있습니다. Native 갱신에는 SKILL/원격 작업 경로를 사용하세요. Cadence 설치/라이선스는 포함하지 않습니다.

## 지원 범위와 검증

[지원 범위·손실 코드·검증 방법](docs/SUPPORT.md)을 확인하세요. 특히 `SIGNED_VOID_FALLBACK`, `INVALID_PLANE_TOPOLOGY`, `TRACE_ATTRIBUTES_NOT_EXPORTED`, `VIA_DRILL_APPROXIMATION`은 물리 설계 검토가 필요한 항목입니다.

대용량 파일은 SQLite와 layer별 임시 파일로 처리합니다. 입력 크기의 수 배에 해당하는 여유 디스크가 필요합니다. GUI 작업은 로딩한 파일의 변경 여부를 확인하고 출력 경로의 상위 폴더에 임시 SPD 사본을 만들어 사용한 뒤 제거합니다. RAM 사용량은 가장 큰 plane/polygon 및 패키지에 영향을 받으며, 보드 전체를 DOM으로 읽지 않습니다. 속도는 형상 복잡도와 디스크에 따라 달라집니다.

개발·설치파일 빌드:

```powershell
python -m pip install -r requirements.txt pytest pyinstaller
python -m pytest -q
.\build.ps1
```

Inno Setup 6 또는 7이 필요합니다. GitHub Actions에서도 테스트와 Windows 설치파일을 빌드합니다. 실제 고객 설계 파일은 저장소·설치파일에 포함하지 않으며, 테스트는 합성 데이터만 사용합니다.

원격 테스트는 HTTPS, 파일 전달, 작업 상태, 취소, 로그와 결과 수집을 로컬 합성 실행기로 검증합니다. 실제 Windows 테스트 창 두 개로 PID별 메시지 전달도 검증합니다. 이것이 Allegro에서 실제 설계를 재구성했다는 증거는 아닙니다. 실제 워크스테이션에서 생성된 `execution.log`, `result.json`, `result.brd` 및 Allegro DRC/설계 비교로 확인해야 합니다.

## 라이선스와 출처

프로그램 소스는 [MIT](LICENSE)입니다. 공식 IPC XSD와 의존성의 조건은 [THIRD_PARTY.md](THIRD_PARTY.md)에 별도로 표시합니다. 프로그램은 Cadence 또는 IPC의 인증 제품이 아닙니다.
