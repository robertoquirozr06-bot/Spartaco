import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Los permisos (scopes) que necesita Espartaco para Calendar, Tasks y Sheets
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/tasks',
    'https://www.googleapis.com/auth/spreadsheets'
]

# En el contenedor de la nube el token vive en el disco persistente
# (GOOGLE_TOKEN_PATH=/data/token.json), donde cada refresh queda guardado.
# En el PC, sin la variable, sigue siendo token.json en la carpeta actual.
_TOKEN_PATH_ENTORNO = os.getenv('GOOGLE_TOKEN_PATH', '').strip()
TOKEN_PATH = _TOKEN_PATH_ENTORNO or 'token.json'

def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif _TOKEN_PATH_ENTORNO:
            # Sin navegador, run_local_server se quedaria colgado esperando
            # un login que nunca llega. Se reautentica en el PC y se copia
            # el token.json nuevo a esta ruta.
            raise RuntimeError(
                f"No hay un token de Google valido en {TOKEN_PATH}. Reautentica en el PC "
                "(python google_auth.py) y copia el token.json nuevo a esa ruta."
            )
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())

    return creds

if __name__ == '__main__':
    print("Generando el token de acceso para Espartaco...")
    get_credentials()
    print("¡Autenticación exitosa! El archivo token.json ha sido creado.")
