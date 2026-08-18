# BrandsLens — Privacy Policy (DRAFT)

**Same caveat as the Terms of Service: this is a working draft to review with
a qualified lawyer before it governs real customer or personal data,
particularly given BrandsLens processes both your team's account data and
third-party mention data that may include other people's names, opinions,
and social media activity.**

*Last updated: [DATE] · Effective: [DATE]*

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

- **Sub-processors**: hosting provider, Anthropic (for AI classification —
  mention text is sent for analysis; account passwords and payment details
  are never sent to this or any AI provider), Stripe/Paystack (billing),
  email delivery provider (invites and notifications).
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

Depending on your jurisdiction (this section should be adapted per
jurisdiction by your lawyer — NDPA rights for Nigerian data subjects, GDPR
rights if you have EU-based customers or process EU residents' data), you
may have the right to: access the personal data we hold about you; correct
inaccurate data; request deletion; object to or restrict certain processing;
and receive your data in a portable format. Contact [PRIVACY EMAIL] to
exercise any of these rights.

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
Data Protection contact: [PRIVACY EMAIL]
[Registered address]

---

### Notes for your lawyer (delete before publishing)

- Confirm whether you need to register with Nigeria's Data Protection
  Commission (NDPC) as a data controller/processor under the NDPA 2023, and
  whether the scale of mention-data collection triggers any additional
  compliance obligations (e.g., a Data Protection Impact Assessment).
- If any customers or the personal data of any mention subjects are EU-based,
  confirm whether GDPR applies and whether a Data Processing Agreement
  template is needed for Customers (since Customers act somewhat like
  controllers of their own workspace's mention data, with BrandsLens as
  processor — this controller/processor relationship should be reviewed and
  formalized in a DPA, not just this policy).
- Section 3's framing of mention data collection (legitimate interest,
  public-data-only) is the standard approach used by social listening tools
  generally, but should be validated against current NDPA/GDPR guidance
  before this goes live, particularly regarding the boundary between
  "publicly available" and any content gathered via Telegram channel
  monitoring, which sits in a greyer area than open web/social scraping.
