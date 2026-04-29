# Trading Bot Ops 🤖

Sistema de gestión operativa para trading algorítmico. Herramienta de trabajo interna del equipo (arquitecto + trader).

---

## Archivos del proyecto

| Archivo | Descripción |
|---------|-------------|
| `index.html` | Hub de navegación principal |
| `proceso.html` | Roadmap completo (6 fases + mini-proyectos M-01 a M-08) |
| `estrategia-spec.html` | Template de especificación formal por estrategia |
| `tracker.html` | Tracker de estado de los 8 mini-proyectos |
| `feedback.html` | Herramienta del arquitecto: análisis, recomendaciones, feedback de código |
| `obv_macd_adx_all_resultado.html` | Backtest: OBV + MACD Divergencia + ADX (todos los instrumentos) |
| `touch_turn_v3_backtest.html` | Backtest: Touch & Turn Scalper v3 |
| `obv_macd_adx_bot_final.py` | Código fuente del bot en vivo (MT5 Python) |

---

## Setup inicial en GitHub (una sola vez)

### Paso 1 — Descargar los archivos desde Claude

Desde la conversación en Claude, descarga los 9 archivos que aparecen en el output. Colócalos todos en una carpeta nueva en tu computador, por ejemplo:

```
# Mac / Linux
~/Documents/trading-bot-ops/

# Windows
C:\Users\TuNombre\Documents\trading-bot-ops\
```

La carpeta debe verse así antes de continuar:
```
trading-bot-ops/
├── index.html
├── proceso.html
├── estrategia-spec.html
├── tracker.html
├── feedback.html
├── obv_macd_adx_all_resultado.html
├── touch_turn_v3_backtest.html
├── obv_macd_adx_bot_final.py
└── README.md
```

---

### Paso 2 — Instalar Git (si no lo tienes)

Abre una terminal y verifica:
```bash
git --version
```

Si no está instalado:
```bash
# Mac (con Homebrew)
brew install git

# Windows: descargar instalador desde https://git-scm.com
# Durante la instalación, dejar todas las opciones por defecto
```

---

### Paso 3 — Configurar tu identidad en Git

Ejecuta estos dos comandos **desde cualquier carpeta** (son configuración global):
```bash
git config --global user.name "Tu Nombre"
git config --global user.email "tu@email.com"
```

---

### Paso 4 — Crear el repositorio en GitHub

1. Ir a [github.com](https://github.com) → botón verde **New** (o **+ → New repository**)
2. **Repository name:** `trading-bot-ops`
3. **Visibility:** Private *(recomendado para código de trading)*
4. **NO** marcar ninguna de las opciones de inicialización (sin README, sin .gitignore, sin licencia)
5. Click en **Create repository**
6. GitHub te mostrará una página con instrucciones — déjala abierta, la necesitas en el paso siguiente

---

### Paso 5 — Inicializar Git en tu carpeta y subir

**Abre una terminal y navega a tu carpeta:**
```bash
# Mac / Linux
cd ~/Documents/trading-bot-ops

# Windows (PowerShell o CMD)
cd C:\Users\TuNombre\Documents\trading-bot-ops
```

**Verifica que estás en la carpeta correcta** (deberías ver los archivos):
```bash
# Mac / Linux
ls

# Windows
dir
```

**Ahora ejecuta estos comandos, uno por uno, siempre dentro de esa misma carpeta:**
```bash
# Inicializar git en esta carpeta
git init

# Agregar todos los archivos al seguimiento
git add .

# Crear el primer commit
git commit -m "feat: proyecto inicial completo"

# Conectar con GitHub (reemplaza TU_USUARIO con tu usuario de GitHub)
git remote add origin https://github.com/TU_USUARIO/trading-bot-ops.git

# Renombrar la rama principal a "main"
git branch -M main

# Subir todo a GitHub
git push -u origin main
```

Cuando Git pida usuario y contraseña:
- **Usuario:** tu usuario de GitHub
- **Contraseña:** NO es tu contraseña de GitHub — es un **Personal Access Token** (ver abajo)

**Crear un Personal Access Token:**
1. GitHub → tu foto de perfil (arriba derecha) → **Settings**
2. Menú lateral izquierdo → **Developer settings** (al fondo)
3. **Personal access tokens → Tokens (classic) → Generate new token (classic)**
4. Note: `trading-bot-ops`
5. Expiration: `No expiration` (o la fecha que prefieras)
6. Scopes: marcar solo **repo**
7. Click **Generate token**
8. **Copia el token ahora** — no lo podrás ver de nuevo
9. Úsalo como contraseña cuando Git lo pida

---

### Paso 6 — Activar GitHub Pages

1. Ir a tu repositorio en GitHub: `github.com/TU_USUARIO/trading-bot-ops`
2. Click en **Settings** (menú superior del repo)
3. Menú lateral izquierdo → **Pages**
4. En "Source": seleccionar **Deploy from a branch**
5. Branch: **main** · Folder: **/ (root)**
6. Click **Save**
7. Esperar ~2 minutos
8. Tu sitio estará disponible en: `https://TU_USUARIO.github.io/trading-bot-ops/`

---

## Flujo de trabajo diario con Claude Code

### Instalar Claude Code (una sola vez)

```bash
# Requiere Node.js instalado (https://nodejs.org)
npm install -g @anthropic-ai/claude-code
```

### Clonar el repo en una máquina nueva

Si cambias de computador o lo instalas en otro equipo, en vez de copiar archivos haz esto **desde la carpeta donde quieras trabajar**:

```bash
# Primero navega a donde quieres que quede la carpeta, por ejemplo:
cd ~/Documents

# Luego clona (esto crea la subcarpeta trading-bot-ops automáticamente)
git clone https://github.com/TU_USUARIO/trading-bot-ops.git

# Entrar a la carpeta
cd trading-bot-ops
```

### Abrir Claude Code

**Siempre desde dentro de la carpeta del proyecto:**
```bash
cd ~/Documents/trading-bot-ops
claude
```

### Pedirle cambios a Claude Code

Una vez dentro de Claude Code, describe el cambio en lenguaje natural:

```
"Actualiza el tracker: cambia M-02 a estado completado con progreso 100%"

"Agrega en feedback.html una nueva observación en vivo:
 fecha: hoy, símbolo: DE40, título: 5 wins consecutivos en apertura Londres,
 descripción: ..., acción: solo observación"

"En estrategia-spec.html actualiza el ADX Threshold de OBV+MACD de 25 a 28"
```

### Subir los cambios a GitHub

Después de que Claude Code edite los archivos, **desde la misma carpeta** del proyecto:

```bash
# Ver qué cambió
git diff

# Agregar los cambios
git add .

# Commit con descripción
git commit -m "feat: actualizar tracker M-02 completado"

# Subir a GitHub (GitHub Pages se actualiza en ~1 minuto)
git push
```

También puedes pedirle a Claude Code que haga el commit y push directamente:
```
"Guarda los cambios y súbelos a GitHub con el mensaje: actualizar M-02"
```

---

## Compartir con el trader

El trader solo necesita esta URL para ver el sistema completo desde cualquier navegador:
```
https://TU_USUARIO.github.io/trading-bot-ops/
```

No necesita instalar nada ni tener cuenta en GitHub.

---

## Troubleshooting

**GitHub Pages no muestra cambios después del push:**
- Esperar 1-2 minutos
- Hard refresh en el navegador: `Cmd+Shift+R` (Mac) o `Ctrl+Shift+R` (Windows)
- Verificar en Settings → Pages que el último deployment fue exitoso (ícono verde)

**Error `remote: Repository not found`:**
- Verificar que el nombre del repo en GitHub coincide exactamente con el de la URL
- Verificar que el Personal Access Token tiene el scope `repo`

**Error `src refspec main does not match any`:**
```bash
# Verificar en qué rama estás
git branch
# Si dice "master" en vez de "main":
git branch -M main
git push -u origin main
```

**Error `Updates were rejected` al hacer push:**
```bash
# Primero traer los cambios remotos
git pull origin main
# Resolver conflictos si los hay, luego:
git push
```
