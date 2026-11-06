# 지원 범위 및 결과 해석

## 무엇을 변환하는가

| SPD 정보 | 출력 | 제한 / 로그 |
|---|---|---|
| Signal/Medium 층과 두께 | Layer / Stackup | 제조 재료 사양·공차는 추정하지 않음. 원래 속성은 JSON에 기록 |
| 양의 Polygon/Box/Circle | Contour / Polygon | 자기 교차 등 잘못된 topology는 경고 |
| 음의 Polygon/Circle | 같은 net의 포함하는 contour의 Cutout | 포함 관계를 확정할 수 없으면 NEGATIVE Set 및 경고. 원은 곡선으로 유지 |
| Node / Trace | 실제 좌표와 Line | 생략된 폭은 해당 층의 기본 폭. 미지원 arc/taper 속성은 경고하고 직선으로 출력 |
| Via | 각 층 pad와 span별 drill Hole | 알려진 inner radius 우선. 없으면 outer barrel radius를 사용한 근사임을 기록 |
| PadStack | Circle/Square/Rectangle/Polygon, offset, rotation | Circle은 반지름→지름 변환. Square/Box는 전체 폭/높이 |
| Component / Connect | Component / Package / LogicalNet / PinRef | 기존 refDes·part에 맞는 참조 package 우선. 없으면 SPD pin 위치에서 재구성 |

참조 패키지는 이름뿐 아니라 기존 부품의 `refDes`와 `part`를 함께 확인합니다. 같은 part에 여러 패키지 변형이 있어 확정할 수 없으면 특정 변형을 임의 선택하지 않습니다. 참조 부품의 BOM/실장 변형과 SPD-only 패키지의 본체·mount type·bottom mirror는 원본 CAD에서 확인해야 합니다.

현재 프로그램의 목적은 형상 교환과 변경 검토입니다. 출력은 제조 승인 데이터가 아니며, 원본의 DRC·thermal relief·dynamic shape·repour·재료 인증·제조 공차를 복원하지 않습니다. Padstack 속성, 원본 재료 값, 변환 건수, 경고는 보고서에서 확인할 수 있습니다.

## 보고서

- `result.xml.log`: 변환 상태, 건수, 손실/경고 코드, 최초 예시와 입력 행 번호.
- `result.xml.report.json`: 모든 경고 유형별 발생 횟수, 최대 5개 예시, 원본 메타데이터, schema 검증 결과, layer/identifier 대응표.
- `identifier_mapping`: 공식 XSD에서 허용되지 않는 괄호·슬래시·대괄호 등이 있는 식별자의 원본→출력 이름. 모든 정의와 참조를 같은 이름으로 변환합니다. 원래 이름은 이 대응표에 보존됩니다.
- `allegro_import_verified: false`: XSD 검증만으로 native Allegro 의미 보존을 확인했다고 표시하지 않습니다.

경고 예시 개수는 메모리와 로그 크기를 제한하기 위해 제한됩니다. 각 코드의 전체 발생 횟수는 집계합니다. 원본 SPD는 변경하지 않으므로 전체 원본 데이터는 항상 입력 파일에 남습니다.

| 주요 코드 | 의미 |
|---|---|
| `BOARD_OUTLINE_UNAVAILABLE` | 유효한 보드 외곽선 없음; copper bounding box로 대체하지 않음 |
| `SIGNED_VOID_FALLBACK` / `INVALID_PLANE_TOPOLOGY` | void 포함 관계/자기 교차를 확인해야 함 |
| `SHAPE_POLARITY_ASSUMED` | Shape 레코드에 `+`/`-` 부호가 없어 양극으로 간주함. 부호가 이름의 일부인 net(`USB_D-` 등)은 부호를 극성과 구분할 수 없으므로, 그런 net이 Node/Trace/Via 목록에 실제로 존재하면 변환을 중단함. 반대로 `VCC-` 같은 이름이 Shape 레코드에만 있고 Node/Trace/Via에는 없으면 대조할 근거가 없어 net `VCC`의 음극 도형으로 읽습니다 |
| `NODE_PAD_NOT_DEFINED` / `PACKAGE_PIN_WITHOUT_PAD` | 지정된 층의 pad 정의가 없어 해당 도형 누락 |
| `TRACE_ATTRIBUTES_NOT_EXPORTED` | 기본 직선/고정 폭 이외의 속성을 출력하지 않음 |
| `VIA_DRILL_APPROXIMATION` | finished drill과 barrel radius가 같은지 확인 필요 |
| `REFERENCE_SCHEMA_NORMALIZED` | exporter의 도형 참조/짧은 polyline을 동등한 표준 표현으로 변경 |
| `IPC_IDENTIFIER_ENCODED` | 식별자를 공식 XSD의 허용 문자 집합으로 변환 |

오류나 해석할 수 없는 기본 좌표/연결, 존재하지 않는 node, 서로 다른 층을 연결하는 Trace, 비동축 via, schema 불일치는 변환을 중단합니다. 불완전한 새 XML을 정상 결과로 발표하지 않습니다.

## 원본 BRD로 돌아가는 경로

BRD는 비공개 binary database입니다. binary라는 사실만으로 암호화 여부를 단정할 수 없습니다. 이 프로그램은 BRD 바이트를 임의 수정하지 않습니다.

Allegro 24.1에 설치되는 `share/pcb/batchhelp/ipc2581_in.txt`에 문서화된 명령:

```text
ipc2581_in <xml> -x -g -i <original.brd> -o <new.brd>
```

`-x`는 stackup, `-g`는 layer features입니다. import 결과는 제조/비교 층으로 취급해야 하며, 기존 ETCH/부품/배선을 갱신한 것으로 간주해서는 안 됩니다. Native plane 수정은 원본 BRD와 Cadence의 지원 API/편집 작업이 추가로 필요합니다. Decap은 OptimizePI report 기반 back-annotation과 구별해야 합니다.

## 검증 근거

1. 작은 합성 SPD로 단위·좌표·정확한 원형 void·via span·부품·net·누락 기록·덮어쓰기 방지를 테스트합니다.
2. 생성 XML은 [공식 IPC-2581B1 XSD](https://webstds.ipc.org/2581/IPC-2581B1.xsd)로 검사합니다. root `revision="B"`는 Amendment 1에서도 유지합니다.
3. 실제 예제의 각 패키지 변형을 참조하는 변환을 별도로 검사합니다. 실제 설계 파일과 해당 XML은 공개 저장소에 올리지 않습니다.
4. XSD 유효성은 문법·타입·일부 참조를 검사합니다. 전기적 연결성, 제조 가능성, Cadence import의 native 의미까지 증명하지 않습니다. [IPC Consortium 설명](https://www.ipc2581.com/ipc-2581-file-validation-tool/)도 이 경계를 명시합니다.

런타임 산출물은 인접 로그를 검토하고, plane 면적/void, 패드, 부품 방향, net 연결을 원본 CAD와 비교한 후 사용하세요.

2026-09-21 로컬 검증에서 합성 SPD → XSD-valid XML → 설치된 Allegro `ipc2581_in` 24.1S008 → 새 BRD의 실행을 확인했습니다(종료 코드 0). 이는 importer가 파일을 수용하고 BRD를 생성했다는 검증이며, native ETCH/부품의 의미 보존 검증은 아닙니다.
