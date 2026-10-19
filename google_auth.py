import os.path
import shutil
import sys
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Los permisos (scopes) que necesita Espartaco para Calendar, Tasks, Sheets y Gmail.
# gmail.modify es el minimo que deja poner etiquetas a los mensajes (gmail.labels
# solo administra las etiquetas en si) y cubre tambien leer y crear borradores.
# Tambien permitiria enviar o mandar a la papelera: que el bot nunca lo haga lo
# garantiza gmail_tools.py, que no tiene ninguna funcion para eso.
#
# OJO al agregar un scope: el token se refresca pidiendo esta lista, asi que si
# el token.json no lo tiene, el refresh falla (invalid_scope) y se cae TODO
# Google, no solo lo nuevo. Primero se reautoriza (--reautorizar) y se copia el
# token a la VM; recien despues se despliega el codigo con el scope nuevo.
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/tasks',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/gmail.modify',
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
                "(python google_auth.py --reautorizar) y copia el token.json nuevo a esa ruta."
            )
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, 'w') as token:
            token.write(creds.to_json())

    return creds


def reautorizar():
    """Pide un token nuevo por el navegador aunque ya exista uno.

    get_credentials() no sirve para esto: si hay un token.json con
    refresh_token lo refresca (o falla refrescandolo) y nunca abre el
    navegador, asi que no hay forma de pedir un scope nuevo ni de reponer un
    token revocado. Aca se salta esa rama, y el token.json actual solo se
    reemplaza (dejando respaldo) cuando ya hay credenciales validas: si el
    login se interrumpe a medias, el token anterior queda intacto.
    """
    if _TOKEN_PATH_ENTORNO:
        raise RuntimeError("--reautorizar necesita navegador: se corre en el PC, no en el contenedor.")

    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    creds = flow.run_local_server(port=0)
    concedidos = creds.granted_scopes or creds.scopes or []
    if isinstance(concedidos, str):
        concedidos = concedidos.split()
    faltantes = set(SCOPES) - set(concedidos)
    if faltantes:
        raise RuntimeError(
            "Google no concedio todos los permisos (falta marcar la casilla en la pantalla de "
            f"consentimiento): {', '.join(sorted(faltantes))}. El token anterior no se toco."
        )

    temporal = TOKEN_PATH + '.nuevo'
    with open(temporal, 'w') as token:
        token.write(creds.to_json())
    if os.path.exists(TOKEN_PATH):
        shutil.copy2(TOKEN_PATH, TOKEN_PATH + '.bak')
    os.replace(temporal, TOKEN_PATH)
    return creds


if __name__ == '__main__':
    if '--reautorizar' in sys.argv:
        print("Abriendo el navegador para autorizar a Espartaco (Calendar, Tasks, Sheets y Gmail)...")
        reautorizar()
        print(f"¡Listo! {TOKEN_PATH} reemplazado (el anterior quedo en {TOKEN_PATH}.bak).")
    else:
        print("Generando el token de acceso para Espartaco...")
        get_credentials()
        print("¡Autenticación exitosa! El archivo token.json ha sido creado.")
