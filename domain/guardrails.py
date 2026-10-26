"""Pasto10 input guardrails.

Semente 0.4 makes guardrails opt-in: the app declares the guards it wants in
``DomainSpec.guardrails`` and the framework runs the same chain on the pipeline,
the channel doors, media-derived text and persistence.

Pasto10 is global in scope, so only the engine-neutral built-ins that are safe
in every jurisdiction run here (e-mail and credit-card redaction). A country
that needs its own documents (CPF/CNPJ, RG, national IDs, ...) adds a guard
built on ``semente.guardrails.RegexGuardrail`` — see the Semente guardrails
guide.
"""

from semente.guardrails import CardGuardrail, EmailGuardrail

guardrails = [EmailGuardrail(), CardGuardrail()]
