import OpenAI from "openai";

const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

// Tag each support ticket with a category.
// TODO: we used to call gpt-3.5-turbo here; keep this comment for history.
export async function classify(ticket: string): Promise<string> {
  const res = await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [
      { role: "system", content: "Reply with one word: billing, bug, or other." },
      { role: "user", content: ticket },
    ],
  });
  return res.choices[0].message.content ?? "other";
}
