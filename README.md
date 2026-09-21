# BRD-SPD-IPC2581

**PowerSI SPD에서 IPC-2581 XML 또는 Allegro용 SKILL 스크립트를 생성하는 Windows 프로그램입니다.**
라이선스가 있는 별도 워크스테이션의 Allegro를 노트북에서 실행하고, 진행 로그와 결과를 회수할 수 있습니다. GUI와 CLI를 제공합니다.

> **이 프로그램은 원본 Allegro BRD를 완전히 재구성하는 도구가 아닙니다.**
> SKILL 경로는 **동일 보드의 원본 BRD**를 바탕으로 SPD의 plane을 native static shape로 반영합니다. 원본의 동적 plane 생성 규칙은 SPD에서 복원할 수 없습니다. IPC 경로는 제조/비교용 형상 교환입니다. 스크립트 생성, Allegro 실행 완료, 설계 검토 완료는 서로 구분하여 기록합니다.

## 설치와 실행

1. [Releases](https://github.com/yunhyok/BRD-SPD-IPC2581/releases)의 `BRD-SPD-IPC2581-Setup-0.2.0.exe`를 노트북과 워크스테이션에 설치합니다. 별도 Python 설치는 필요하지 않습니다.
2. 워크스테이션에서 **Workstation Agent**를 열어 Allegro 실행 파일을 지정하고 서버를 시작합니다. 화면에 표시된 접속 토큰과 인증서 지문을 노트북에 입력합니다.
3. 노트북의 **BRD-SPD-IPC2581 → 원격 작업**에서 워크스테이션 IP, SPD, 동일 보드의 원본 BRD를 지정하여 실행합니다. 진행 로그를 확인하고 완료 후 결과 ZIP을 다운로드합니다.

IP는 언제든 바꿀 수 있습니다. 인증서와 토큰은 워크스테이션의 작업 폴더에 보관되므로 IP만 변경된 경우 다시 발급할 필요가 없습니다. Allegro 기본 경로는 `C:\Cadence\SPB_24.1\tools\bin\allegro.exe`입니다. 에이전트가 실행 중인 Windows 사용자에게 Allegro 라이선스와 보드 접근 권한이 있어야 합니다.

**IPC-2581 변환** 탭은 기존과 동일하게 사용합니다. **SKILL 생성** 탭에서는 워크스테이션에 접속하지 않고 스크립트 묶음만 생성할 수 있습니다. 자세한 절차는 [원격 사용 안내](docs/REMOTE_USER_GUIDE.md), [native SKILL 지원 범위](docs/NATIVE_SKILL.md)를 참고하세요.

설치파일은 코드 서명이 없는 초기 릴리스입니다. 설치 경로는 기본적으로 `%LOCALAPPDATA%\Programs\BRD-SPD-IPC2581`입니다.

## 변환 경로

```mermaid
flowchart LR
    L[노트북: SPD + 원본 BRD] -->|인증된 HTTPS| W[워크스테이션 에이전트]
    W --> S[SKILL 생성 + 손실 보고서]
    S --> A[Allegro 24.1 실행]
    A --> R[새 result.brd + 실행 로그]
    R -->|조회·다운로드| L
```

원격 통신은 인증서 지문을 고정한 HTTPS와 접속 토큰을 사용합니다. 워크스테이션에서 허용할 노트북 IP를 선택적으로 제한할 수 있습니다. 노트북은 서버를 열지 않습니다. 작업마다 별도 디렉터리와 원본의 복사본을 사용하고 Allegro는 한 번에 한 작업만 실행합니다.

IPC 경로에서는 참조 XML의 동박/배선 형상을 복사하지 않습니다. 물리 형상은 SPD에서 생성합니다. 참조 XML의 BOM은 변경 전 정보일 수 있으므로 복사하지 않고 로그에 기록합니다. 참조 XML이 없거나 외곽선이 비어 있으면 보드 크기를 추측하지 않습니다.

## CLI

설치된 `brd-spd-ipc2581-cli.exe`를 사용하거나 소스에서 다음 명령을 실행합니다.

```powershell
python -m pip install -r requirements.txt
python launch_cli.py convert edited.spd -o result.xml --template original.xml
python launch_cli.py inspect edited.spd -o inventory.json
python launch_cli.py validate result.xml --xsd schemas/IPC-2581B1.xsd
python launch_cli.py skill edited.spd -o skill-bundle --layer TOP --net VCC
```

워크스테이션에서 에이전트를 실행하는 CLI도 제공합니다.

```powershell
python launch_cli.py serve --host 0.0.0.0 --port 8765 --allegro-exe "C:\Cadence\SPB_24.1\tools\bin\allegro.exe"
python launch_cli.py remote --host WORKSTATION_IP --token-file pairing-token.txt --fingerprint CERT_SHA256 health
python launch_cli.py remote --host WORKSTATION_IP --token-file pairing-token.txt --fingerprint CERT_SHA256 submit edited.spd --base-brd original.brd
```

`remote status`, `remote logs`, `remote cancel`, `remote download`에 반환된 작업 ID를 전달합니다. 토큰은 명령행 문자열 대신 파일 또는 `BRDSPD_TOKEN` 환경변수로 제공하여 셸 기록에 남기지 않습니다.

변환은 항상 **새 출력 파일**을 요구합니다. 입력 파일이나 기존 결과를 덮어쓰지 않습니다. 실패하면 종료 코드 `1`과 실패 보고서를 남깁니다. 경고가 있는 정상 변환은 종료 코드 `0`, 보고서 상태 `completed_with_warnings`입니다.

라이선스가 있는 Allegro의 제조/비교용 import를 실행하려면:

```powershell
python launch_cli.py import-brd result.xml -o comparison.brd --base-brd original.brd --cadence-root "C:\Cadence\SPB_24.1"
```

이 IPC import 명령은 원본 대신 새 BRD를 출력하지만 native plane/decap 갱신용 명령은 아닙니다. Native 갱신에는 위의 SKILL/원격 작업 경로를 사용하세요. Cadence 설치/라이선스는 포함하지 않습니다.

## 지원 범위와 검증

[지원 범위·손실 코드·검증 방법](docs/SUPPORT.md)을 확인하세요. 특히 `SIGNED_VOID_FALLBACK`, `INVALID_PLANE_TOPOLOGY`, `TRACE_ATTRIBUTES_NOT_EXPORTED`, `VIA_DRILL_APPROXIMATION`은 물리 설계 검토가 필요한 항목입니다.

대용량 파일은 SQLite와 layer별 임시 파일로 처리합니다. 입력 크기의 수 배에 해당하는 여유 디스크가 필요합니다. RAM 사용량은 가장 큰 plane/polygon 및 패키지에 영향을 받으며, 보드 전체를 DOM으로 읽지 않습니다. 속도는 형상 복잡도와 디스크에 따라 달라집니다.

개발·설치파일 빌드:

```powershell
python -m pip install -r requirements.txt pytest pyinstaller
python -m pytest -q
.\build.ps1
```

Inno Setup 6 또는 7이 필요합니다. GitHub Actions에서도 테스트와 Windows 설치파일을 빌드합니다. 실제 고객 설계 파일은 저장소·설치파일에 포함하지 않으며, 테스트는 합성 데이터만 사용합니다.

원격 테스트는 HTTPS, 파일 전달, 작업 상태, 취소, 로그와 결과 수집을 로컬 합성 실행기로 검증합니다. 이것이 Allegro에서 실제 설계를 재구성했다는 증거는 아닙니다. 실제 워크스테이션에서 생성된 `execution.log`, `result.json`, `result.brd` 및 Allegro DRC/설계 비교로 확인해야 합니다.

## 라이선스와 출처

프로그램 소스는 [MIT](LICENSE)입니다. 공식 IPC XSD와 의존성의 조건은 [THIRD_PARTY.md](THIRD_PARTY.md)에 별도로 표시합니다. 프로그램은 Cadence 또는 IPC의 인증 제품이 아닙니다.
