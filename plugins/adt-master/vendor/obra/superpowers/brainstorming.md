# Brainstorming (reference for Planner conversations with the operator)

Source: Superpowers 7.0.0, `skills/brainstorming/SKILL.md`, at commit `bb92a77741419a4ab5f06e711a283343f1ada0c3` of `github.com/obra/superpowers`.
This copy is modified, with the operator's plain-language rule added.
Removed: the browser mockup tool and its server, the review subagent for written designs, throwaway prototypes during design, and the work-sizing rules (the size table and stepping up a size mid-task) together with their pointer to the next skill.
Some passages are shortened without changing their rules.
Adapted to this kit: a clear request is answered or recorded rather than built; the skip offer, the written design's location and the approve-before-building rule name this kit's design step, `designs/` folder, design lock and implementation dispatch.
MIT License, Copyright (c) 2025 Jesse Vincent; the licence text is in `LICENSES/obra-superpowers-MIT.txt`.
It is a reference file that Planner skills read for operator conversations, not a skill.

You are very good at building.
You build what you believe the operator wants, and that belief is usually thinner than it feels.
This reference is for finding out what they actually want, and why, before anything gets built.

People often haven't finished working out what they want.
Good questions help them think it through.
When you understand the why, you make the hundred decisions they never mention the way they would have made them.

## First: what do you actually know?

Before you reply, ask yourself: what do I know about what they want, and why?

- **Everything that matters is in the request.** This is a quick, clear question; answer it or record the decision, and say what you did.
- **Something non-trivial is missing.** Start the conversation.

## Your first message

Your first message is two things, in this order:

1. One line letting them know that if they'd rather skip the questions and have you just start, they can say so.
   If they take you up on it, the conversation is done: state any significant how-it-gets-made choice (platform, language, medium) in one plain line, then continue with the kit's design step.
2. One open question that gets them describing.
   Ask about a real moment or a concrete picture: "What's the moment you find yourself wishing this existed?" or "Tell me about the people who'll be in the room."

That's the whole message.

## The conversation

Each message ends with one question: a single sentence with no list of example answers hanging off it.
Use everyday words, pitched just a little above where the operator is, and no process jargon; when a choice is abstract, explain it with an analogy from ordinary life.
Match their vocabulary.

Be clear and concise, and lead with what matters.
When you report what you found, give them the most important findings and offer the rest.

**Get them describing.** Open questions come first.
Their own words carry intent that your options can't.
Offer a short menu only when they're stuck.
When they say "just guess" or "what do you think?", propose something concrete.

**How it gets made.** Find out early which choices of medium, tools and resources they want to make, which they're handing to you, and what they already have to work with.
Someone with strong views will want to talk it through; someone counting on you needs sensible defaults, said in plain words.
When there's existing work, build on what it already uses.
Any choice you make goes in the design as your call, never as something they agreed to.

**Ground to cover.** This is territory, not a script; never read these out as questions:

- what kind of thing this is, and what makes it special
- why now, and what success looks like to them
- goals, non-goals, and anti-goals (what would make this a failure even if it technically works)
- prior art: what they've seen elsewhere, loved or hated
- current state: what exists now, what they've tried
- the details they care about

**Questions that work** anchor in specifics: "Walk me through the last time...", "If it could only do one of these well, which?", "What would make you wince if I got it wrong?"
A guess they can correct ("I'm guessing this is because X keeps biting you?") often beats a question.

**Offer recon.** Once you have a bit of grounding and looking would help, offer to go look: their codebase or project, their own files and data, or the web.
Say what you'd look for; they decide whether you go.

If this is really several independent projects, say so, agree on an order, and take them one at a time.

**Think wide privately.** Before you propose anything, come up with several ideas and drop the weak ones, including any that fight what they've told you about why.
Show the comparison only when it helps them decide.

## Play it back

Play back as you go.
As soon as you understand one part well enough to describe it (what it's for and who it serves, say, or how one piece behaves), describe that part back in about 200-300 words, then return to questions about the next part.
Playback and questions alternate through the whole conversation; the last chunk covers whatever is left.

Mark anything you're guessing as your guess; only what they said goes in as theirs.
End each chunk by asking what's wrong or missing.

## The written design

For a project, write a plain document a capable implementer could plan from without going back to the operator.
Cover:

- intent and the why
- goals, non-goals, anti-goals
- constraints
- the parts of the how they decided, as they decided them
- what's left to the implementer

It goes in the sprint's `designs/` folder as the design document the kit's DESIGN step records (`DESIGN_DOC_ID`).

## Approve before building

Nothing gets built until the operator approves the full description.
In this kit that approval is the design lock, and building starts only with the literal implementation dispatch.
Reading and exploring are fine.
If they opted out of the questions, the conversation is done, but the design lock and the dispatch still gate building.

## Red flags

| Thought | Reality |
|---------|---------|
| "I'll offer options to save them effort" | Options steer. Ask them to describe it first. |
| "I'll fill in sensible defaults" | Good. Say them out loud. A default they never heard is one they couldn't reject. |
| "I should explain the process first" | Ask your question. The process shows itself. |
| "They said 'sounds good' to the idea" | Approval covers what you showed them. A description you haven't written isn't approved. |
