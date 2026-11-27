import fs from "node:fs";
import OpenAI from "openai";

const client = new OpenAI();

export async function transcribe(path: string) {
  return client.audio.transcriptions.create({ file: fs.createReadStream(path), model: "whisper-1" });
}

export async function translateToEnglish(path: string) {
  // POST /v1/audio/translations
  return client.audio.translations.create({ file: fs.createReadStream(path), model: "whisper-1" });
}
