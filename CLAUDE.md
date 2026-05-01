# Trading Bot Ops — Contexto de sesión

## Qué es este proyecto

Sistema de análisis de bots de trading. El trader sube archivos (Python, CSV) via `index.html`, Claude los analiza, y los resultados se renderizan en `feedback.html` desde `data.json`.

- **GitHub Pages**: repo `TuQRApp/trading-bot-ops`, branch `main`
- **Worker**: `trading-upload.nestragues.workers.dev`
- **Archivos clave**: `data.json` (fuente de verdad), `feedback.html` (análisis), `index.html` (dashboard + upload)

---

## INICIO DE SESIÓN — hacer siempre esto primero

Al iniciar cualquier sesión en este directorio:

1. Leer `data.json` local
2. Buscar grupos con `"status": "pending"`
3. Si hay grupos pendientes → **anunciarlo al trader y comenzar el análisis automáticamente**, sin esperar que el trader lo pida
4. Si no hay grupos pendientes → continuar normal

### Cómo analizar un grupo pending

1. Identificar `folder` y `files` del grupo en data.json
2. Fetchear cada archivo desde GitHub:
   `https://raw.githubusercontent.com/TuQRApp/trading-bot-ops/main/Archivos/{folder}/{filename}`
3. Leer y analizar el código/CSV
4. Generar borrador de m1–m4 (ver esquema abajo)
5. Presentar el borrador al trader para revisión y corrección
6. Una vez aprobado, escribir el análisis final a data.json via `PUT https://trading-upload.nestragues.workers.dev/data`
7. Actualizar `status` a `"activo"`

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
