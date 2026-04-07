Backend working:
1. Feature Explanation (Marketing-Friendly)
The in-app assistant lets people talk in everyday language about food and health: finding recipes, checking nutrition, swapping ingredients, seeing what they have planned or already eaten, and starting actions like logging a meal or building a plan. It remembers the current conversation for a short time, can tell when someone is asking for themselves versus the whole household, and—for answers that need the recipe and nutrition catalog—it pulls real items from your product’s data before wording the reply. Actions that actually change someone’s plan or log are held for a clear “yes” first, so the app does not surprise users with changes they did not intend.

2. How It Works (Simple)
You send a message from the B2C app. The nutrition service receives it together with who you are (and optional household or member context the app already knows).

The system opens or continues a short-lived chat session so follow-ups like “what about soy?” or “any other ideas?” still make sense.

It figures out what you meant. Common phrases are recognized quickly; trickier wording is interpreted with help from a language model so the request is classified into something the product knows how to handle (recipes, nutrition questions, substitutions, and similar).

If you are agreeing or declining a pending step (for example after it offered to log a meal or create a plan), it routes that separately: a clear “yes” tells the main app what to run; a “no” cancels politely.

If you asked to see your own structured data—like this week’s plan, what you ate recently, or a simple summary of how you have been doing nutritionally—it reads that from your records and answers in plain text, without inventing details.

If you asked something that needs the recipe and nutrition library, it searches that library in several complementary ways, applies your preferences and safety needs (such as diets and allergens), and may use optional healthy-eating guidance when the deployment is set up for it. A language model then writes the reply using only that gathered material and the recent conversation, so the tone is natural but the facts come from your catalog and rules.

For simple social messages (hello, goodbye, what can you do, or topics outside food and nutrition), it uses short, fixed replies—sometimes personalized with your name or diet style when the app has that information.

It sends back the message text, what kind of request was detected, session id for the next message, and when relevant, structured extras (for example recipe titles for the UI to show as cards).

3. Why It’s Valuable
You do not have to learn menus and filters—you can ask the way you would ask a friend who knows the app.
Answers about recipes and nutrients are tied to what actually lives in the product, which reduces made-up meals or wrong numbers compared with a generic chat tool.
It respects your profile (and, when the question is phrased that way, household or family-style needs) so suggestions stay relevant and safer around restrictions you have saved.
Risky changes need an explicit okay, so logging meals or generating plans feels controlled rather than accidental.
Follow-up questions work because the assistant can use the last few turns of conversation, including turning vague follow-ups into a clear question when needed.
Problem it helps solve: People want fast, personalized food and nutrition help without hunting through every screen—and they need to trust that numbers and recipe names match the app, not a generic internet answer.

4. What Makes It Smart
Two layers of “understanding”: fast pattern matching for typical requests, with a language model when the same intent is buried in longer or unusual wording.
Conversation-aware follow-ups: short or pronoun-heavy messages can be merged with what you were just discussing (including “more options” after a substitution-style answer).
Grounded answers for catalog-style questions: the model is instructed to stick to retrieved nutrition and recipe content rather than freelancing facts.
Household-aware phrasing: questions framed as “for the family,” “for the kids,” or “for me” can steer whose profile logic applies.
Guardrails on actions: the assistant separates “show me or tell me” from “change my data,” and only the latter waits for confirmation before the main backend acts.

Read more:
Meet your smart in-app nutrition assistant—designed to feel less like a tool and more like a knowledgeable companion. Simply ask questions in your own words, whether you’re looking for recipe ideas, checking nutrition, swapping ingredients, or planning meals, and get answers tailored to your preferences, health needs, and household context. Unlike generic chat tools, every response is grounded in your app’s real data; so recipes, nutrients, and recommendations are accurate, relevant, and trustworthy. It remembers your conversation for smooth follow-ups, keeps suggestions aligned with your dietary goals, and ensures any changes - like logging meals or creating plans—only happen with your clear approval. The result? Faster decisions, personalized guidance, and a seamless way to navigate your nutrition journey without digging through menus.
