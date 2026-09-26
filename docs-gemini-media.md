# Lyria + Omni via the Interactions API (notes used by server.js)

Source: user-supplied summary of ai.google.dev docs (music-generation, omni), 2026-09-26. The docs show only the Python SDK; the REST shape used here (`POST /v1beta/interactions`, snake_case fields) is inferred from it. If it's wrong, run `node --env-file=.env probe.js media`.

- **Call:** `client.interactions.create(model, input, response_format?, previous_interaction_id?)`
- **input:** a string, or an array of `{type:'text', text}` / `{type:'image', mime_type, data}` / `{type:'video', uri}` blocks
- **Response:** `steps[] → {type:'model_output', content:[{type:'audio'|'video'|'text', data|uri, mime_type}]}`. The SDK shortcuts are `output_audio` / `output_video`.
- **Omni (`gemini-omni-1.1-flash`):** `response_format {type:'video', aspect_ratio:'16:9'|'9:16', resolution:'360p'|'720p'|'1080p'|'4k', delivery:'uri'?}`. The inline payload limit is about 4MB (use `delivery:'uri'` for >720p). Edits: `previous_interaction_id` + a short instruction, e.g. "… Keep everything else the same." `store=false` disables later edits.
- **Lyria 3.5 (`lyria-3.5`):** one-shot. MP3 by default. Accepts up to 10 input images. Length is set in the prompt. Say "Instrumental only, no vocals" for an underscore. `lyria-3-clip-preview` = fixed 30s, faster.
- **Lyria RealTime (`models/lyria-realtime-exp`):** WebSocket streaming, 48kHz PCM. Not used here.
