#!/usr/bin/env node

import {
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { extname, resolve } from "node:path";

function loadEnv(file = ".env") {
  const envPath = resolve(process.cwd(), file);
  if (!existsSync(envPath)) return;

  const lines = readFileSync(envPath, "utf8").split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const match = trimmed.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!match) continue;

    const [, key, rawValue] = match;
    let value = rawValue.trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }

    if (process.env[key] === undefined) {
      process.env[key] = value;
    }
  }
}

loadEnv();

const apiKey =
  process.env.MINIMAX_API_KEY ||
  process.env.MINIMAX_APIKEY ||
  process.env.API_KEY;

if (!apiKey) {
  console.error("Missing API key. Add MINIMAX_API_KEY=... to .env");
  process.exit(1);
}

const endpoint =
  process.env.MINIMAX_ENDPOINT ||
  "https://api.minimaxi.com/v1/chat/completions";
const model = process.env.MINIMAX_MODEL || "MiniMax-M3";

const args = process.argv.slice(2);
const shouldRecordFull = args.includes("--record-full");
const shouldRecord = shouldRecordFull || args.includes("--record");
const positionalArgs = args.filter(
  (arg) => arg !== "--record" && arg !== "--record-full",
);

const imageInput =
  positionalArgs[0] ||
  "https://filecdn.minimax.chat/public/fe9d04da-f60e-444d-a2e0-18ae743add33.jpeg";
const question =
  positionalArgs.slice(1).join(" ") || "What does this image show?";

function mimeTypeFromPath(filePath) {
  const ext = extname(filePath).toLowerCase();
  if (ext === ".jpg" || ext === ".jpeg") return "image/jpeg";
  if (ext === ".png") return "image/png";
  if (ext === ".webp") return "image/webp";
  if (ext === ".gif") return "image/gif";
  return "application/octet-stream";
}

function toImageUrl(input) {
  if (/^https?:/i.test(input)) {
    return {
      url: input,
      log: {
        input,
        source: "remote_url",
      },
    };
  }

  if (/^data:/i.test(input)) {
    return {
      url: input,
      log: {
        input: "inline_data_url",
        source: "data_url",
        dataUrlLength: input.length,
        preview: `${input.slice(0, 80)}...`,
      },
    };
  }

  const filePath = resolve(process.cwd(), input);
  if (!existsSync(filePath)) {
    console.error(`Image not found: ${input}`);
    process.exit(1);
  }

  const mimeType = mimeTypeFromPath(filePath);
  const bytes = readFileSync(filePath);
  const base64 = bytes.toString("base64");
  const dataUrl = `data:${mimeType};base64,${base64}`;
  const stats = statSync(filePath);

  return {
    url: dataUrl,
    log: {
      input,
      source: "local_file",
      filePath,
      mimeType,
      fileBytes: stats.size,
      base64Chars: base64.length,
      dataUrlChars: dataUrl.length,
      preview: `${dataUrl.slice(0, 80)}...`,
    },
  };
}

function imageDataForRecord(value) {
  if (typeof value !== "string") return value;
  if (shouldRecordFull) return value;
  if (!value.startsWith("data:")) return value;
  return `${value.slice(0, 80)}...[redacted ${value.length} chars]`;
}

function writeRecord(record) {
  mkdirSync("records", { recursive: true });
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const path = `records/minimax-vl-${timestamp}.json`;
  writeFileSync(path, `${JSON.stringify(record, null, 2)}\n`);
  return path;
}

const image = toImageUrl(imageInput);
const imageUrl = image.url;

const body = {
  model,
  thinking: {
    type: "adaptive",
  },
  messages: [
    {
      role: "user",
      content: [
        {
          type: "text",
          text: question,
        },
        {
          type: "image_url",
          image_url: {
            url: imageUrl,
          },
        },
      ],
    },
  ],
  max_completion_tokens: 500,
};

const startedAt = new Date().toISOString();
const requestLog = {
  startedAt,
  recordMode: shouldRecordFull ? "full" : "redacted",
  endpoint,
  model,
  image: image.log,
  request: {
    method: "POST",
    headers: {
      Authorization: "Bearer [redacted]",
      "Content-Type": "application/json",
    },
    body: {
      ...body,
      messages: body.messages.map((message) => ({
        ...message,
        content: message.content.map((part) =>
          part.type === "image_url"
            ? {
                ...part,
                image_url: {
	                  ...part.image_url,
	                  url: imageDataForRecord(part.image_url.url),
	                },
	              }
            : part,
        ),
      })),
    },
  },
};

if (shouldRecord) {
  console.error("[record] image source:", image.log.source);
  console.error("[record] mode:", shouldRecordFull ? "full" : "redacted");
  if (image.log.source === "local_file") {
    console.error("[record] local image:", image.log.filePath);
    console.error("[record] file bytes:", image.log.fileBytes);
    console.error("[record] base64 chars:", image.log.base64Chars);
  }
}

const response = await fetch(endpoint, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${apiKey}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify(body),
});

const text = await response.text();
let data;
try {
  data = JSON.parse(text);
} catch {
  data = text;
}

const record = {
  ...requestLog,
  finishedAt: new Date().toISOString(),
  response: {
    ok: response.ok,
    status: response.status,
    statusText: response.statusText,
    body: data,
  },
};
if (shouldRecord) {
  const recordPath = writeRecord(record);
  console.error("[record] saved:", recordPath);
}

if (!response.ok) {
  console.error(`MiniMax request failed: ${response.status} ${response.statusText}`);
  console.error(typeof data === "string" ? data : JSON.stringify(data, null, 2));
  process.exit(1);
}

const answer = data?.choices?.[0]?.message?.content;
console.log(answer || JSON.stringify(data, null, 2));
