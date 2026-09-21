# 노트북에서 Allegro 워크스테이션 제어

프로그램의 **Help / 도움말** 메뉴에서 원하는 [HTML 사용 안내](help/index.html)를, **F1**에서 현재 역할에 맞는 안내를 프로그램 안의 내장 뷰어로 열 수 있습니다. Chrome, Edge, WebView2 또는 인터넷 설치는 필요하지 않습니다. 뷰어의 목차와 문서 내 링크, 글자 축소/확대 기능으로 필요한 항목을 확인하세요.

## 준비

두 PC에 Setup 설치 파일로 같은 버전의 `BRD-SPD-IPC2581.exe`를 설치합니다. Allegro와 라이선스는 워크스테이션에만 필요합니다. 앱은 처음에 원격 Allegro 작업 탭을 열며, **노트북 (작업 요청)** 또는 **워크스테이션 (Allegro 실행)** 역할을 선택합니다. 마지막 역할 선택은 유지됩니다. 기본 Allegro 실행 파일은 `C:\Cadence\SPB_24.1\tools\bin\allegro.exe`이며 화면에서 변경할 수 있습니다.

이 프로그램의 기본 작업은 **동일 보드의 원본 BRD에 SPD의 plane 형상을 반영하여 새 BRD를 만드는 것**입니다. 일반적인 SPD만으로 원본의 모든 native 설계 객체와 규칙을 복구하는 기능은 아닙니다. 지원 범위는 [NATIVE_SKILL.md](NATIVE_SKILL.md)를 확인하세요.

## 워크스테이션에서 한 번 설정

1. 메인 앱에서 **워크스테이션 (Allegro 실행)** 역할을 선택합니다. 이 선택만으로 Agent가 시작되지는 않습니다.
2. Allegro 실행 파일, 작업 폴더, 수신 IP와 포트를 지정합니다. 모든 로컬 네트워크 인터페이스를 사용하려면 수신 IP를 `0.0.0.0`으로 두고 기본 포트 `8765`를 사용합니다.
3. 필요하면 허용할 노트북 IP를 입력하고 **Agent 시작**을 누릅니다. 화면의 접속 토큰과 인증서 SHA-256 지문을 노트북 프로그램에 옮깁니다. 기존 **BRD-SPD-IPC2581 Workstation Agent** 별도 실행 파일도 같은 방식으로 계속 사용할 수 있습니다.

토큰과 인증서는 해당 작업 폴더에 유지됩니다. 작업 폴더를 바꾸면 별도 서버 설정으로 취급됩니다. 토큰은 이 에이전트의 작업을 실행·취소·조회할 수 있는 접속 자격입니다. 프로그램은 방화벽 규칙이나 Windows 자동 시작 설정을 임의로 변경하지 않습니다. 기존 인트라넷 통신이 가능하더라도 새 포트가 차단되어 있으면 해당 PC의 관리 정책에 맞게 허용해야 합니다.

## 노트북에서 작업 실행

1. 메인 프로그램의 원격 작업 화면에서 **노트북 (작업 요청)** 역할을 선택합니다. 현재 워크스테이션 IP/호스트명과 포트를 입력하고, 에이전트가 표시한 접속 토큰과 인증서 지문을 입력한 뒤 연결을 확인합니다.
2. 수정한 SPD를 **찾아보기…**로 선택하여 읽거나 경로를 직접 입력하고 **SPD 불러오기**를 누릅니다. 로딩에 성공하면 대상 레이어·NET의 **목록에서 선택…**에서 전체 선택/선택 해제, Ctrl/Shift 다중 선택을 할 수 있습니다. 파일 경로를 바꾸면 이전 선택을 초기화하고 다시 로딩해야 합니다. 실행 중인 Allegro에 반영하려면 워크스테이션 작업 관리자의 **세부 정보**에서 `allegro.exe` PID를 확인하고, 노트북의 **Allegro PID**에 직접 입력합니다. **PID 확인**으로 대상 실행 파일과 생성 시각을 확인하세요. PID를 비우면 같은 보드의 원본 BRD를 선택하여 별도 Allegro를 실행합니다. 레이어와 NET을 지정하면 해당 범위의 plane을 반영합니다. 입력란을 비우면 지원되는 SPD plane 전체가 대상입니다.
3. 파일 전송과 실행을 시작합니다. 화면의 작업 ID를 보관하면 프로그램을 다시 열어도 서버에 남아 있는 작업 상태와 로그를 조회할 수 있습니다.
4. 완료 후 새 경로에 결과 ZIP을 다운로드합니다. `result.brd`와 생성·실행 로그, 구조화된 보고서를 함께 확인합니다. 실패한 작업에서도 로그 묶음을 회수할 수 있습니다.

작업 취소는 해당 작업에만 적용됩니다. 다른 Allegro 세션을 종료하지 않습니다. 파일 전송 중의 실패나 네트워크 끊김이 서버의 작업 성공을 뜻하지는 않습니다. 다시 접속한 뒤 작업 ID로 상태를 확인하세요. 파일 업로드의 중간 바이트 재개는 지원하지 않습니다.

## IP가 변경되었을 때

노트북에서 워크스테이션 주소를 새 IP로 바꾸면 됩니다. 같은 에이전트 작업 폴더를 사용하면 토큰과 인증서 지문은 유지됩니다. 워크스테이션에서 노트북 IP 허용 목록을 설정했다면 노트북 IP 변경도 목록에 반영해야 합니다. 고정된 특정 인터페이스 IP에 서버를 바인딩했다면 새 주소로 수정하고 서버를 재시작하세요. `0.0.0.0` 수신 설정은 특정 주소에 종속되지 않습니다.

## 여러 Allegro 중 하나를 선택할 때

PID는 **워크스테이션의 Allegro PID**이며 PowerSI 또는 노트북 프로세스의 PID가 아닙니다. 대상 Allegro에 SPD와 같은 보드가 열려 있어야 합니다. PID만으로 서로 다른 보드의 동일성을 판단할 수 없으므로 파일을 직접 확인하세요. 선택 창에서 진행 중인 명령이나 모달 대화상자를 마친 후 실행하고, 작업 중에는 그 창을 직접 편집하지 마세요. 에이전트와 Allegro를 같은 Windows 로그인 세션 및 권한 수준으로 실행해야 합니다.

PID 모드는 원본 BRD를 업로드하지 않습니다. 현재 열린 설계의 저장하지 않은 변경까지 포함하여 `session-before.brd`에 백업한 다음 적용합니다. 성공하면 새로운 `result.brd`를 저장하고 해당 보드를 Allegro에 열어 둡니다. 원래 디스크의 BRD는 덮어쓰지 않습니다. 결과 ZIP에는 복구용 백업도 들어가므로 BRD 두 개를 저장할 여유 공간이 필요합니다.

PID와 실행 경로·프로세스 생성 시각을 작업 생성, 제출, 명령 전달 시 확인합니다. 종료되거나 재사용된 PID, 대상 창이 없거나 여러 개인 경우 실행을 중단하며 다른 창으로 자동 전환하지 않습니다. 명령 전달이 시간 초과되면 중복 전송하지 않고 완료 표식을 기다립니다. 60초 동안 스크립트 시작을 확인할 수 없으면 진단 로그와 취소 표식을 남깁니다. 그 이후 늦게 시작한 스크립트도 변경 전에 취소 표식을 확인합니다.

PID 모드의 취소는 SKILL이 다음 취소 확인 지점에서 트랜잭션을 되돌리는 방식입니다. Allegro 프로세스는 종료하지 않으므로 긴 Cadence 함수가 끝날 때까지 취소가 지연될 수 있습니다. 저장 단계에 도달한 뒤의 취소 요청은 적용되지 않을 수 있으며, 실제 성공 결과를 유지합니다. 커밋 이후 파일 저장이 실패하면 열린 설계는 변경된 상태일 수 있습니다. `design_modified`와 실행 로그를 확인하고 필요하면 ZIP의 `session-before.brd`를 별도로 열어 복구하세요.

취소 요청 후 10초 안에 완료를 확인하지 못하거나, 기본 6시간의 관찰 한도에 도달하면 `interrupted`, `native_pending=true`로 표시합니다. 이는 Allegro 작업의 성공·실패를 아직 확인하지 못했다는 뜻입니다. 에이전트 CLI의 `--session-timeout 초`로 관찰 한도를 조정할 수 있습니다. 원격 관찰 오류도 같은 방식으로 남깁니다. 나중에 **기존 작업 받기** 또는 상태 조회를 다시 실행하면 뒤늦게 작성된 native 완료 결과를 반영하고 ZIP을 갱신합니다. 완료 미확인 상태의 ZIP에는 저장 중일 수 있는 백업 BRD를 포함하지 않으며, 최종 결과를 다시 받아야 합니다.

## 출력과 상태 해석

| 산출물 | 용도 |
|---|---|
| `design.il`, `run.scr` | Allegro에서 실행하는 생성 스크립트 |
| `manifest.json` | 생성 옵션과 스크립트 묶음 정보 |
| `generation.log`, `generation.report.json` | 지원하지 않는 항목, 누락 및 생성 형상 통계 |
| `execution.log`, `runner.log` | SKILL 실행 및 Allegro 프로세스 진단 |
| `result.json` | SKILL의 저장 성공 여부 |
| `result.brd` | 성공한 작업이 별도로 저장한 결과 보드 |
| `session-before.brd` | PID 모드에서 변경 전 현재 설계의 복구용 백업 |

새 Allegro 실행 모드는 프로세스 종료 코드, SKILL 결과 표식, 결과 BRD의 존재를 함께 확인합니다. PID 모드는 프로세스를 종료하지 않고 SKILL 완료 표식과 결과 BRD를 확인합니다. 실행 성공은 전기적 타당성이나 DRC 통과를 보증하지 않습니다. 결과 보드를 Allegro에서 열어 변경한 영역, net, void 및 DRC를 확인해야 합니다. 원본과의 전체 동등성은 별도 검증 대상입니다.

## CLI 예시

워크스테이션:

```powershell
brd-spd-ipc2581-cli.exe serve --host 0.0.0.0 --port 8765 --allegro-exe "C:\Cadence\SPB_24.1\tools\bin\allegro.exe"
```

노트북의 PowerShell:

```powershell
$WorkstationAddress = "현재_워크스테이션_IP"
$CertificateFingerprint = "에이전트에_표시된_SHA256_지문"
brd-spd-ipc2581-cli.exe remote --host $WorkstationAddress --token-file pairing-token.txt --fingerprint $CertificateFingerprint health
brd-spd-ipc2581-cli.exe remote --host $WorkstationAddress --token-file pairing-token.txt --fingerprint $CertificateFingerprint submit edited.spd --base-brd original.brd
brd-spd-ipc2581-cli.exe remote --host $WorkstationAddress --token-file pairing-token.txt --fingerprint $CertificateFingerprint check-pid 12345
brd-spd-ipc2581-cli.exe remote --host $WorkstationAddress --token-file pairing-token.txt --fingerprint $CertificateFingerprint submit edited.spd --allegro-pid 12345
brd-spd-ipc2581-cli.exe remote --host $WorkstationAddress --token-file pairing-token.txt --fingerprint $CertificateFingerprint status JOB_ID
brd-spd-ipc2581-cli.exe remote --host $WorkstationAddress --token-file pairing-token.txt --fingerprint $CertificateFingerprint logs JOB_ID
brd-spd-ipc2581-cli.exe remote --host $WorkstationAddress --token-file pairing-token.txt --fingerprint $CertificateFingerprint cancel JOB_ID
brd-spd-ipc2581-cli.exe remote --host $WorkstationAddress --token-file pairing-token.txt --fingerprint $CertificateFingerprint download JOB_ID -o result.zip
```

토큰 파일에는 토큰 한 줄만 넣습니다. 공용 저장소나 보고서에 접속 토큰을 포함하지 마세요. 로컬 SKILL 생성과 IPC 변환에는 원격 접속 정보가 필요하지 않습니다.

## 검증 경계

자동 테스트는 인증된 HTTPS 전송, 잘못된 토큰·인증서 차단, 작업 상태 전이, 취소와 결과 회수를 합성 데이터로 검증합니다. 실제 Allegro 24.1의 라이선스, SKILL 실행 호환성, 실제 보드의 DRC는 워크스테이션에서 실행해야 검증할 수 있습니다. 이 둘의 결과를 혼동하지 않도록 보고서에 구분해 남깁니다.
