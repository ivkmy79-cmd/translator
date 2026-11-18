"""테스트는 서버 PC 의 실제 .env 값에 의존하면 안 되므로 환경을 먼저 고정한다.

load_dotenv() 는 이미 설정된 환경변수를 덮어쓰지 않으므로, app 임포트 전에 여기서 정한
값이 우선한다.
"""
import os

os.environ["AZURE_SPEECH_KEY"] = ""
os.environ["AZURE_SPEECH_REGION"] = "koreacentral"
# 서버 PC 의 실제 자격증명 파일을 읽지 않도록 참조를 끊는다.
os.environ["AZURE_CREDENTIALS_FILE"] = ""
# 서버 PC 의 .env 에 접속 코드가 있어도 테스트는 코드 없는 상태에서 시작한다.
os.environ["ROOM_PASSCODE"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["FLOOR_LIMIT_SECONDS"] = "2"
os.environ["FLOOR_WARNING_SECONDS"] = "1"
