# 📂 PROJECT CONTEXT: Asistente Personal IA "Espartaco"

## 🎯 Objetivo del Proyecto
Desarrollar y ejecutar localmente un agente de Inteligencia Artificial llamado **Espartaco**, diseñado para operar como un asistente personal y administrativo avanzado. Se comunica a través de un **grupo de Telegram**, gestiona el tiempo, coordina tareas delegadas y audita finanzas mediante la integración con Google Workspace y capacidades de búsqueda web en tiempo real vía MCP.

---

## 🛠️ Stack Tecnológico y Arquitectura
- **Orquestador Principal:** Python (código nativo, arquitectura modular basada en scripts asíncronos).
- **Interfaz de Usuario:** Telegram Bot API (operando en un grupo, usando `python-telegram-bot` vía Long Polling).
- **Cerebro / Razonamiento:** Gemini API (modelo `gemini-1.5-flash` con soporte para *Function Calling*).
- **Autenticación:** Google OAuth 2.0 (escritorio, gestionado mediante `credentials.json` y `token.json`).
- **Ecosistema Google Workspace:**
  - **Google Calendar:** Gestión de citas y eventos con un planificador interno (`APScheduler`) para alertas proactivas.
  - **Google Tasks:** Sistema de memoria y asignación de tareas mediante etiquetas de metadatos (`[Agente]`, `[Persona 1]`, `[Persona 2]`).
  - **Google Sheets:** Lectura y escritura de registros financieros y reportes de pagos.
- **Motor de Búsqueda:** Servidor MCP (Model Context Protocol) en Node.js + Playwright para automatizar Google Chrome de manera local.

---

## 📊 Estado Actual del Desarrollo (Progreso)
1. **Infraestructura Base:** Entorno virtual (`venv`) configurado y dependencias base instaladas en `C:\Users\rober\Spartacus\asistente_ia`.
2. **Core de Comunicación:** Script base (`main.py`) implementado para conectar Telegram con Gemini.
3. **Credenciales y Seguridad:** Proyecto en Google Cloud (`Espartaco-IA`) creado con las APIs de Calendar, Tasks y Sheets habilitadas, y credenciales de OAuth 2.0 preparadas.
4. **Próximos Pasos Inmediatos:** Superar la validación de usuario de prueba en Google Cloud para generar el `token.json` y comenzar con la programación de las funciones de Google Workspace.

---

## 👥 Reglas de Negocio y Metadatos
- **El Agente:** Se llama **Espartaco** y responde en contexto de grupo de Telegram.
- **Memoria de Tareas:** Se filtran por etiquetas estrictas en Google Tasks para saber qué debe ejecutar el agente de forma autónoma frente a lo que corresponde a cada miembro del equipo.
- **Finanzas:** Espartaco tiene la capacidad de leer y escribir en hojas de cálculo para auditar gastos e ingresos a petición del grupo.
