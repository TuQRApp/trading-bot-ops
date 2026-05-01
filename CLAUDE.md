# Trading Bot Ops — Contexto de sesión

## Qué es este proyecto

Sistema de análisis de bots de trading. El trader sube archivos (Python, CSV) via `index.html`, Claude los analiza, y los resultados se renderizan en `feedback.html` desde `data.json`.

- **GitHub Pages**: repo `TuQRApp/trading-bot-ops`, branch `main`
- **Worker**: `trading-upload.nestragues.workers.dev`
- **Archivos clave**: `data.json` (fuente de verdad), `feedback.html` (análisis), `index.html` (dashboard + upload)

---

## INICIO DE SESIÓN — hacer siempre esto primero

Al iniciar cualquier sesión en este directorio, leer `data.json` y actuar según los estados:

---

### Si hay grupos con `status: "pending"`

1. Fetchear cada archivo del grupo desde GitHub:
   `https://raw.githubusercontent.com/TuQRApp/trading-bot-ops/main/Archivos/{folder}/{filename}`
2. Leer y analizar el código/CSV en profundidad
3. Generar borrador **completo** de m1–m4 sin pedir confirmación (ver esquema abajo)
4. Agregar al grupo: `trader_notes: ""`, `revision_submitted: false`, `rereview_requested: false`
5. Escribir el grupo con `status: "en_revision"` a data.json via `PUT https://trading-upload.nestragues.workers.dev/data`
6. Enviar email al trader (`nestragues@icloud.com`) via curl a Resend API:
   - Subject: `[Trading Bot] Borrador listo para revisar — {badge}`
   - Body: link a `https://tuqrapp.github.io/trading-bot-ops/feedback.html`

---

### Si hay grupos con `status: "pendiente_final"`

El trader revisó el borrador. Los campos `correction` en las cards y `trader_notes` tienen su feedback.

1. Leer el grupo completo (borrador + corrections + trader_notes + rereview_notes si aplica)
2. Incorporar todas las correcciones del trader en el análisis final
3. Limpiar campos de revisión en el grupo: vaciar `correction` en cada card, `trader_notes: ""`, `revision_submitted: false`, `rereview_requested: false`, `rereview_notes: ""`
4. Escribir el grupo con `status: "activo"` a data.json via `PUT /data`
5. Enviar email al trader (`nestragues@icloud.com`) via curl a Resend API:
   - Subject: `[Trading Bot] Análisis finalizado — {badge}`
   - Body: link a `https://tuqrapp.github.io/trading-bot-ops/feedback.html`

---

### Si hay grupos con `status: "en_revision"` sin acción tuya

El trader está revisando. No hacer nada.

### Si no hay grupos en ninguno de estos estados

Continuar normal con lo que el usuario pida.

---

## Esquema de análisis (m1–m4)

### m1 — Calidad del backtest
```json
{
  "type": "quality",
  "last_updated": "YYYY-MM-DD · HH:MM · archivo",
  "score": {
    "valor": 7.5,
    "max": 10,
    "label": "Aceptable con reservas",
    "bullets": [
      { "type": "ok",   "text": "descripción de punto positivo" },
      { "type": "warn", "text": "descripción de advertencia" },
      { "type": "bad",  "text": "descripción de problema grave" }
    ]
  },
  "metrics": [
    {
      "id": "QM-01",
      "label": "Win Rate",
      "value": "62.3%",
      "status": "ok",
      "note": "explicación breve"
    }
  ]
}
```
`status` de cada metric: `"ok"` | `"warn"` | `"bad"`

Si no hay datos de backtest (solo código sin CSV): usar `"type": "empty"` con `empty_title` y `empty_desc`.

### m2 — Recomendaciones
Array de rec-cards:
```json
[
  {
    "id": "R-01",
    "tipo": "param",
    "tipo_label": "Parámetro",
    "prioridad": "alta",
    "estado": "pendiente",
    "title": "Título de la recomendación",
    "desc": "Descripción detallada con el problema y la solución propuesta.",
    "comment": ""
  }
]
```
`tipo`: `"param"` | `"logic"` | `"risk"` | `"data"` | `"meta"`  
`prioridad`: `"alta"` | `"media"` | `"baja"`  
`estado`: `"pendiente"` | `"implementado"` | `"descartado"`

### m3 — Observaciones
Array de obs-cards:
```json
[
  {
    "id": "OBS-001",
    "tipo": "warn",
    "origin": "nombre_archivo.py",
    "title": "Título de la observación",
    "desc": "Descripción detallada.",
    "comment": ""
  }
]
```
`tipo`: `"warn"` | `"error"` | `"info"`

### m4 — Feedback de código
Array de arch-cards:
```json
[
  {
    "id": "H-01",
    "categoria": "bug",
    "title": "Título del hallazgo",
    "desc": "Descripción del problema.",
    "code": "fragmento_de_codigo_relevante()",
    "fix": "propuesta_de_corrección()",
    "comment": ""
  }
]
```
`categoria`: `"bug"` | `"riesgo"` | `"ausencia"` | `"mejora"`  
`code` y `fix`: strings con `\n` para saltos de línea (NO `<br>`, se renderiza con `white-space:pre-wrap`)

---

## Cómo escribir a data.json

```javascript
// Leer SHA actual
GET https://trading-upload.nestragues.workers.dev/data

// Escribir (reemplaza data.json completo)
PUT https://trading-upload.nestragues.workers.dev/data
Content-Type: application/json
Body: { ...data completo con el grupo actualizado }
```

---

## Grupos existentes

| Badge | Nombre | fsh_class | folder | Status |
|---|---|---|---|---|
| FILE-001 | obv_macd_adx_bot_final_v2.py | fsh-py | null (local) | activo |
| FILE-002 | backtest_canal_fib.py + CSVs | fsh-fib | null (local) | activo |
| FILE-003 | bot_canal_fib_v3.py | fsh-v3 | null (local) | activo |

Nuevos grupos subidos via Worker reciben `fsh-a` … `fsh-h` (paleta cíclica).

---

## Notas importantes

- `folder: null` → archivos locales, no en GitHub. No intentar fetchear.
- Trader = Nicolás Estragues (nestragues@gmail.com)
- El análisis es iterativo: siempre presentar borrador → trader corrige → escribir final
- No sobrecargar al trader con demasiadas observaciones a la vez: ir módulo por módulo (m1 → m2 → m3 → m4)
