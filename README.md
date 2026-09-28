# Virtual Harvester

PySide6와 Harvester를 사용하는 카메라 GUI입니다. 가상 GenTL Producer 또는
실제 장비 제조사의 CTI를 선택해 같은 `DeviceManager`로 검색·연결·영상 취득을 수행합니다.
기본 가상 구성은 **프레임그래버 3개 × 카메라 4개 = 12대**이며, GUI는 영상 12개를 표시합니다.

## 설치

프로젝트 루트에서 기존 Miniconda `mvs` 환경에 설치합니다.

```bash
conda activate mvs
python -m pip install -r requirements.txt
```

의존성은 현재 `mvs` 환경(Python 3.14.7)의 설치 버전을 기준으로 고정했습니다.
새 환경이 필요하면 먼저 `conda create -n mvs python=3.14`를 실행하세요.
Python 표준 라이브러리는 별도 설치하지 않습니다.

포함된 `VirtualFG.cti`는 **Linux x86-64용** 공유 라이브러리입니다.
Windows Python에서는 이 바이너리를 사용할 수 없습니다. WSL에서는 Linux의
`mvs` 환경과 Qt 창을 표시할 수 있는 WSLg 등의 디스플레이 환경이 필요합니다.
실제 모드는 운영체제와 프로세스 아키텍처에 맞는 제조사 SDK/드라이버 및 CTI가 필요합니다.

## 실행

```bash
# 기본: 가상 모드
python main.py
python main.py --virtual True

# 실제 장비 모드
python main.py --virtual False --real-cti /absolute/path/to/vendor.cti

# 가상 CTI 위치와 미리보기 전송 FPS 지정
python main.py --virtual True \
  --virtual-cti /absolute/path/to/VirtualFG.cti --preview-fps 15
```

| 옵션 | 기본값 | 용도 |
|---|---|---|
| `--virtual` | `True` | 초기 모드 선택, `True` 또는 `False` |
| `--virtual-cti` | `externals/virtualfg/VirtualFG.cti` | 가상 CTI 경로 |
| `--real-cti`, `--cti` | 없음 | 제조사 CTI 경로 |
| `--preview-fps` | `15` | 카메라별 미리보기 전송 상한, 0 초과 120 이하 |

모드는 GUI 초기 상태에서 선택합니다. 실제 모드로 시작하려면 `--real-cti`가 필요합니다.
가상 모드로 시작해 GUI에서 Hardware를 선택할 경우에도 실제 CTI 경로를 미리 전달해야 합니다.
Browse 버튼은 없으며 기본 경로는 `config/path.py`에서 관리합니다.

### 사용 순서

1. 모드를 선택하고 **Search**로 프레임그래버와 카메라를 검색합니다.
2. **Connect**로 검색된 카메라 전체를 연결합니다.
3. **Start**로 영상을 표시하고 **Stop**으로 취득을 정지합니다.
4. **Disconnect**로 연결을 해제하면 다시 검색할 수 있습니다.

왼쪽은 영상 12개, 오른쪽은 제어·장치 트리·상태·FPS·로그 패널입니다.
오른쪽 패널은 세로로 스크롤합니다. 현재 매니저는 최대 12대까지 연결합니다.
FPS는 연결된 카메라당 평균 수신 FPS이며 미리보기 전송 상한과는 별개입니다.
로그는 화면과 `outputs/YY-MM-DD/UI_Logs.txt`에 기록됩니다.
**Clear Log**는 화면의 로그만 비웁니다.

## 폴더 구조

```text
main.py                         # 실행 옵션, GUI/작업 스레드 연결, 종료 처리
requirements.txt
config/path.py                  # 기본 CTI, UI, 출력 경로
gui/
  mainwidget.py                 # GUI 상태, 버튼, 로그, QImage 표시
  mainwidget.ui                 # Qt Designer UI
device/
  device_manager.py            # 검색, 일괄 연결, 취득, FPS, 장치 정리
externals/virtualfg/
  VirtualFG.cti                 # 실행용 가상 GenTL Producer
  virtualfg.json                # 프레임그래버와 카메라 구성
native/virtualfg/
  build.py                     # Linux CTI 빌드
  virtualfg.cpp                # Producer 구현
  GenTL_v1_6.h                 # GenTL API 헤더
  exports.inc                  # 빌드 시 생성, Git 제외
  vendor/                     # nlohmann/json 헤더와 라이선스
  licenses/                   # GenICam 라이선스
  LICENSE
  THIRD_PARTY_NOTICES.md
outputs/                       # 실행 중 생성, Git 제외
```

`externals/virtualfg`는 CTI와 JSON만 포함하는 실행용 폴더입니다.
두 파일은 Git에 포함하며, 재빌드용 소스와 라이선스는 `native/virtualfg`에 보관합니다.

## 가상 장치 구성과 재검색

`VirtualFG.cti`와 **같은 폴더**의 `virtualfg.json`을 편집합니다.
파일명은 대소문자를 구분하며 현재 작업 디렉터리를 기준으로 찾지 않습니다.

```json
{
  "framegrabbers": [
    {
      "id": "VFG-0001",
      "display_name": "Virtual Board 1",
      "cameras": [
        {
          "id": "VCAM-0001",
          "serial_number": "VC000001",
          "user_defined_name": "Camera 1",
          "vendor": "VirtualFG",
          "model": "VirtualMono8"
        }
      ]
    }
  ]
}
```

- 프레임그래버는 GenTL **Interface**, 카메라는 **Device**에 대응합니다.
- 수량과 연결 관계는 배열로 결정됩니다. 카메라를 옮기려면 해당 항목을 다른 보드 배열로 이동합니다.
- 보드의 `id`, 카메라의 `id`, 카메라의 `serial_number`는 각각 전체 구성에서 중복될 수 없습니다.
- `display_name`은 생략하면 보드 ID, `user_defined_name`은 카메라 ID를 사용합니다.
  `vendor`, `model`의 기본값은 `VirtualFG`, `VirtualMono8`입니다.
- 문자열은 비어 있지 않은 UTF-8 문자열이며 최대 255바이트입니다. 제어 문자는 허용하지 않습니다.
- CTI는 최대 16개 보드와 보드당 최대 16대 카메라, 빈 보드와 빈 전체 목록을 지원합니다.
  GUI 매니저의 연결 한도는 총 12대입니다.
- 파일 누락, 잘못된 형식, 중복 ID, 알 수 없는 필드는 구성 오류입니다.

**JSON 저장 후 다시 Search하면 변경된 구성이 반영됩니다. 재빌드나 재시작은 필요 없습니다.**
취득 중이면 Stop → Disconnect 후 Search하세요. 검색된 상태에서도 다시 Search할 수 있습니다.

Python 매니저는 JSON을 직접 읽지 않습니다. 가상·실제 모두 Harvester를 호출하며,
가상 CTI가 검색 시 JSON을 읽어 Interface/Device 관계를 반환합니다.
장치별 CTI를 따로 만들 필요는 없습니다. 한 Producer가 여러 보드와 카메라를 제공합니다.
가상 CTI는 실제 PCIe/10GigE 드라이버가 아니며 전송 계층 타입은 `Custom`입니다.
실제 장비의 보드·카메라 매핑은 제조사 Producer가 노출하는 Interface/Device 구성에 따릅니다.

### Harvester에서 직접 사용

```python
from pathlib import Path
from harvesters.core import Harvester

with Harvester() as h:
    h.add_file(str(Path("externals/virtualfg/VirtualFG.cti").resolve()),
               check_existence=True, check_validity=True)
    h.update()
    for info in h.device_info_list:
        print(info.parent.id_, info.id_, info.serial_number)

    if h.device_info_list:
        with h.create(h.device_info_list[0]) as camera:
            camera.start()
            with camera.fetch(timeout=2) as buffer:
                component = buffer.payload.components[0]
                image = component.data.reshape(component.height, component.width).copy()
            camera.stop()

    # JSON을 변경한 뒤 다시 호출하면 새 구성을 검색합니다.
    h.update()
```

재검색 전에 취득을 정지하고 버퍼를 반환하세요. `h.update()`는 기존 취득 객체와
인터페이스를 해제하므로 이전 DeviceInfo/ImageAcquirer를 재사용하지 않고 다시 생성합니다.
`device_info_list`는 카메라 목록이므로 빈 보드는 여기에 나타나지 않습니다.
매니저는 빈 보드도 표시하기 위해 Harvester 내부 인터페이스 목록을 참조하므로
Harvester 버전을 변경할 때 검색 동작을 확인해야 합니다.

직접 GenTL C API를 사용하는 경우 `TLUpdateInterfaceList`가 JSON을 다시 읽습니다.
구성이 변경되었다면 해당 System의 열린 Interface를 먼저 닫아야 합니다.
`IFUpdateDeviceList`는 해당 검색 시점의 보드별 카메라 목록을 반환합니다.
서로 다른 Harvester 인스턴스의 열린 장치는 각자의 검색 시점 구성을 유지합니다.

## GUI와 DeviceManager 통신

`ApplicationController`가 GUI와 별도 `QThread`의 `DeviceManager`를 연결합니다.
Harvester 생성·검색·취득·정리는 작업 스레드에서 실행합니다.
매니저 슬롯을 GUI 스레드에서 직접 호출하지 않고 Qt 신호로 요청합니다.

| 요청 | 매니저에 전달하는 값 |
|---|---|
| 검색 | `{"virtual": True}` 또는 `{"virtual": False}` |
| 연결 | `{"framegrabbers": [...], "virtual": True}` |
| 시작·정지·연결 해제·종료 | 인자 없음 |

현재 GUI의 `search_requested`는 인자가 없고 컨트롤러가 선택 모드를 dict로 변환합니다.
GUI 연결 요청은 `{"framegrabbers": self.framegrabbers}`이며 컨트롤러가 모드를 보충합니다.
연결 요청에는 직전 검색 결과의 카메라 전체가 필요합니다.
선택적으로 `"settings": {"PixelFormat": "Mono8", "ExposureTime": 1000}`처럼
공통 GenICam 설정을 넣을 수 있으며 지원하지 않는 설정은 연결 실패로 처리합니다.

완료 신호는 공통 형식의 dict를 전달합니다.

```python
{"success": True, "error": None, "state": "CONNECTED"}
{"success": False, "error": "오류 원인", "state": "SEARCHED"}
```

검색 결과에는 `framegrabbers`가 추가되고 성공 시 `virtual`도 포함됩니다.
각 보드는 `id`, `display_name`, `cameras`, 각 카메라는 `id`, `serial_number`,
`user_defined_name`, `vendor`, `model` 정보를 갖습니다.
컨트롤러는 매니저 결과의 `state`를 GUI에 최종 반영합니다.

| 동작 | 성공 후 상태 | 실패 시 처리 |
|---|---|---|
| 검색 | `SEARCHED` | 검색 실패는 `INITIAL`, 잘못된 요청 순서는 기존 상태 유지 |
| 연결 | `CONNECTED` | 열린 장치를 정리해 `SEARCHED`, 정리 실패는 `ERROR` |
| 시작 | `ACQUIRING` | 부분 시작된 장치를 정리해 `SEARCHED`, 정리 실패는 `ERROR` |
| 정지 | `CONNECTED` | 부분 정지 실패는 `ERROR` |
| 연결 해제 | `SEARCHED` | 부분 해제 실패는 `ERROR` |

GUI는 초기 `INITIAL` 및 연결·시작 진행 상태 `CONNECTING`, `STARTING`도 사용합니다.
취득 중 예외는 `error_occurred(dict)`로 전달합니다.
`ERROR`에서는 연결 해제 또는 종료로 남은 장치를 정리할 수 있습니다.

### 영상과 FPS

- 매니저는 `frame_received(int, QImage, float)`로 카메라 인덱스, 영상, 해당 카메라 수신 FPS를 보냅니다.
- 인덱스는 연결 요청의 보드/카메라 순서대로 0부터 부여합니다.
- 컨트롤러는 GUI 슬롯 `on_frame_received(index, fps, image)`에 맞춰 인자 순서를 바꿉니다.
- 매니저가 버퍼 반환 전에 QImage를 복사·축소하며 GUI 스레드에서 QPixmap으로 변환합니다.
- 기본 미리보기 크기는 최대 640×480, 전송 상한은 카메라별 15 FPS입니다.
- 수신 FPS는 약 1초 간격으로 계산하며 첫 측정 전과 정지 후에는 0입니다.
  `fps_updated(float)`는 연결된 카메라당 평균 수신 FPS입니다.
- 연속 취득으로 사용하며 해당 노드가 있으면 `AcquisitionMode=Continuous`, `TriggerMode=Off`로 설정합니다.
- 지원 형식은 Mono8, Mono16, RGB8, BGR8, RGBa8의 단일 스트림·단일 컴포넌트입니다.
  Bayer 및 packed 10/12-bit 변환은 구현되어 있지 않습니다. 가상 CTI는 Mono8을 생성합니다.

창을 닫거나 Ctrl+C를 누르면 매니저 정리를 요청하고 작업 스레드가 종료된 뒤 앱을 닫습니다.
정리에 실패하면 오류를 표시하고 창 종료를 다시 시도할 수 있습니다.
실제 장비의 취득 및 정리 동작은 제조사 CTI와 해당 카메라로 별도 검증해야 합니다.

## 가상 CTI 재빌드

JSON만 변경할 때는 빌드하지 않습니다. Producer 소스를 변경할 때 Linux C++17 컴파일러가 필요합니다.
GenTL 및 JSON 헤더는 소스에 포함되어 있습니다.

```bash
conda activate mvs
python native/virtualfg/build.py
# 디버그 빌드
python native/virtualfg/build.py --debug
```

`CXX` 환경변수로 컴파일러를 지정할 수 있으며 기본값은 `g++`입니다.
성공한 결과만 `externals/virtualfg/VirtualFG.cti`에 반영하고 JSON은 보존합니다.
새 바이너리를 사용하려면 실행 중인 앱을 종료한 뒤 다시 실행하세요.

가상 카메라는 `Width`, `Height`, `PixelFormat`, `AcquisitionFrameRate`, `ExposureTime`,
`TriggerMode`, `TriggerSource`, `TriggerSoftware`, `AcquisitionStart`, `AcquisitionStop`
노드를 제공합니다. `DeviceSerialNumber`, 읽기 전용 `DeviceUserID`는 JSON을 반영합니다.
영상 생성 환경변수는 `VFG_TILE_WIDTH`, `VFG_TILE_HEIGHT`, `VFG_STEP_PIXELS`,
`VFG_CIRCLE_COUNT`, `VFG_CIRCLE_RADIUS`, `VFG_SCENE_PGM`입니다.
장면의 행은 보드 순서, 열은 보드 내 카메라 순서이며 전체 열 수는 보드별 카메라 수의 최댓값입니다.
장치 개수는 환경변수가 아닌 JSON에서 지정합니다.

Producer 및 포함된 외부 헤더의 라이선스 정보는
[LICENSE](native/virtualfg/LICENSE),
[THIRD_PARTY_NOTICES.md](native/virtualfg/THIRD_PARTY_NOTICES.md)를 참고하세요.
