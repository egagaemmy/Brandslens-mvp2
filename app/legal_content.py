"""app/legal_content.py — the Terms of Service and Privacy Policy text,
embedded directly here rather than read from a separate legal/ folder at
deploy time.

This exists because the previous file-based approach failed in production
twice: the legal/ folder is a separate top-level directory, easy to miss
during a manual copy-into-git step, and each time it was missing this page
went blank instead of showing real content. Embedding the text directly in
the Python package eliminates that failure mode structurally — this file
deploys exactly when the rest of the app does, every time, since the whole
application depends on the app/ package existing at all.
"""

TERMS_OF_SERVICE = """\
# BrandsLens — Terms of Service

*Last updated: 1 September 2026 · Effective: 1 September 2026*

## 1. Who this agreement is with

These Terms govern access to and use of BrandsLens (the "Service"), provided
by BrandsLens Limited ("BrandsLens," "we," "us"). By creating an account,
you agree to these Terms on behalf of yourself and the organization you
represent (the "Customer").

## 2. What BrandsLens does

BrandsLens monitors publicly available online sources (news, social media,
forums, and similar) for mentions of a Customer's brand, provides AI-assisted
classification of those mentions, and offers workflow tools ("Media Room")
for escalating and responding to significant findings. **BrandsLens is a
monitoring and workflow tool. It is not a law firm, is not providing legal
advice, and does not guarantee detection of every mention, threat, or
instance of fraud.**

## 3. Accounts, organizations, and team members

- One organization corresponds to one Customer's private account. Data
  within an organization is not accessible to any other organization.
- The person who creates the account is the **Owner** and is responsible for
  the organization's use of the Service, including the conduct of anyone
  they or a Team Lead invites.
- Owners and Team Leads may invite additional members and are responsible
  for removing access promptly when someone leaves the Customer's
  organization.
- You are responsible for maintaining the confidentiality of your password
  and for all activity under your account.

## 4. Subscription, billing, and cancellation

- Plans and pricing are as published at brandslens.com/pricing and may
  change with 30 days' notice for existing subscribers.
- Subscriptions renew automatically (annually, monthly, or daily, as
  selected) until cancelled. Cancelling stops future renewal; it does not
  refund the current period unless required by applicable consumer law.
- On cancellation, your organization's data is retained in a read-only state
  for 30 days before deletion, so an accidental cancellation can be reversed.
- Free trials, where offered, convert to a paid subscription unless
  cancelled before the trial ends — the exact terms will be shown at signup.

## 5. Acceptable use

You will not use the Service to: monitor or target an individual for
harassment; submit content to the Media Room's escalation or regulator
notification workflow that you know to be false; attempt to access another
organization's data; or use the Service in a way that violates applicable
law, including data protection law in your jurisdiction.

## 6. AI-generated content

Statement drafts, classifications, and other AI-generated output are
**drafts and suggestions only**. A human must review any statement, incident
classification, or escalation before it is sent, published, or relied upon
for a legal or regulatory purpose. BrandsLens is not responsible for
consequences arising from AI-generated content used without human review.

## 7. Data sources and accuracy

Mentions are gathered from third-party public sources that BrandsLens does
not control. We do not guarantee the completeness, accuracy, or availability
of any particular source, and a given platform's terms of service, technical
changes, or access restrictions may affect what can be monitored at any
time.

## 8. Intellectual property

BrandsLens retains all rights to the Service itself. The Customer retains all
rights to their own workspace configuration, keywords, and any content they
author within the Service (such as drafted statements). Mentions collected
from public sources remain the property of their original authors/platforms;
BrandsLens's use of them is for monitoring and classification purposes only.

## 9. Confidentiality

Each party will keep the other's non-public information confidential and use
it only to perform this agreement. This does not restrict disclosure
required by law or a valid regulatory or court order.

## 10. Disclaimers and limitation of liability

The Service is provided "as is." To the maximum extent permitted by law,
BrandsLens disclaims warranties of merchantability, fitness for a particular
purpose, and non-infringement, and is not liable for indirect, incidental,
or consequential damages, or for any amount exceeding fees paid in the
preceding 12 months. **Nothing in this section limits liability that cannot
be limited under applicable law** (including, where applicable, liability
for gross negligence or willful misconduct).

## 11. Termination

Either party may terminate for convenience with 30 days' notice, or
immediately for material breach that isn't cured within 15 days of notice.
BrandsLens may suspend an account immediately for suspected fraud, non-payment,
or activity that risks the security or data of other Customers.

## 12. Governing law and disputes

These Terms are governed by the laws of the Federal Republic of Nigeria,
without regard to conflict-of-laws principles.

## 13. Changes to these Terms

We may update these Terms; material changes will be notified to account
Owners with reasonable notice before taking effect.

## 14. Contact

BrandsLens Limited
Email: info@brandslens.com
"""

PRIVACY_POLICY = """\
# BrandsLens — Privacy Policy

*Last updated: 1 September 2026 · Effective: 1 September 2026*

## 1. What this policy covers

This Privacy Policy explains what personal data BrandsLens collects, why, and
what rights you have over it. It applies to (a) account data for people who
sign up for or are invited to use BrandsLens, and (b) mention data collected
from public sources that may incidentally include personal data about third
parties (e.g., the author of a social media post).

## 2. Data we collect about you (account holders and team members)

- **Account data**: name, email address, password (stored as a salted
  Argon2 hash — we never store or can retrieve your actual password).
- **Organization data**: company name, sector, plan, billing status.
- **Usage data**: login timestamps, actions taken within the Service
  (for the Media Room's audit trail — this is a core safety feature, not
  incidental tracking, and is disclosed here for that reason).
- **Billing data**: processed by our payment providers (Stripe and/or
  Paystack) — BrandsLens does not store your card number.

## 3. Data we collect about third parties (mention data)

To provide the monitoring service, BrandsLens collects publicly available
content from news sites, social media, forums, and similar sources that
mentions a Customer's brand. This may incidentally include:

- Public usernames, display names, and post content of people who mention
  the monitored brand
- Publicly visible engagement metrics (follower counts, reach estimates)

**We do not collect private messages, private accounts, or any content not
publicly accessible at the time of collection**, except for content
voluntarily forwarded to a Customer's tip line by that Customer's own team
or contacts, which is treated as the Customer's own reporting, not as data
BrandsLens independently gathered.

## 4. Why we process this data (legal basis)

- Account data: to perform our contract with you (providing the Service)
  and for our legitimate interest in account security and support.
- Mention data: our legitimate interest, on behalf of the Customer, in
  monitoring publicly available information about their own brand for
  reputational and fraud-prevention purposes. Where a mention includes
  identifiable personal data about its author, that data is processed only
  to the extent necessary to assess and respond to the mention (e.g.,
  determining if a post constitutes fraud targeting the Customer's
  customers) — not for any other purpose.

## 5. Who we share data with

- **Sub-processors**: our hosting providers, Anthropic (for AI classification
  — mention text is sent for analysis; account passwords and payment details
  are never sent to this or any AI provider), Stripe/Paystack (billing),
  and our email delivery provider (invites and notifications).
- We do not sell personal data. We do not share mention data with any party
  other than the Customer whose brand it concerns, except as required by law
  or a valid regulatory/court order.

## 6. Data retention

- Active account data is retained for as long as the account is active.
- On cancellation, organization data is retained read-only for 30 days
  (so an accidental cancellation can be reversed) and then deleted.
- Mention data is retained per the Customer's plan and configuration;
  Customers can request deletion of specific incidents at any time.

## 7. Your rights

Depending on your jurisdiction, you may have the right to: access the
personal data we hold about you; correct inaccurate data; request deletion;
object to or restrict certain processing; and receive your data in a
portable format. Contact info@brandslens.com to exercise any of these
rights.

**A note on third-party mention data**: if you are the author of a public
post that has been captured as a mention within a Customer's BrandsLens
workspace and you wish to exercise a data right over that specific mention,
contact us and we will work with the relevant Customer, since they control
that workspace's data.

## 8. International data transfers

If BrandsLens's infrastructure or sub-processors are located outside your
country, your data may be transferred internationally. We take steps to
ensure any such transfer has an appropriate legal basis (e.g., standard
contractual clauses where required under GDPR).

## 9. Security

We use industry-standard measures including encrypted connections (HTTPS),
salted password hashing (Argon2), and role-based access controls that
prevent one organization's team members from accessing another
organization's data. No system is perfectly secure; we will notify affected
Customers without undue delay in the event of a data breach affecting their
data, consistent with applicable law.

## 10. Children's data

BrandsLens is a business tool not directed at or intended for use by
children, and we do not knowingly collect personal data from children.

## 11. Changes to this policy

We will notify account Owners of material changes to this policy with
reasonable notice before they take effect.

## 12. Contact

BrandsLens Limited
Data Protection contact: info@brandslens.com
"""
