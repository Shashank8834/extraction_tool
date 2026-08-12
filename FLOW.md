# How the whole thing works, end to end

Three systems, one chain. Nothing is re-typed between them.

```
  BCL team                Client                  BCL team              Documents
     │                       │                       │                      │
 1. create link ──▶ 2. fill form + upload ──▶ 3. download master ──▶ 4. generate
  intake.bclworkspace.in      (autofilled)        (one .xlsx)         (llp-gen)
```

## 1. The team creates an intake link

Log in at **https://intake.bclworkspace.in/admin/login**, type the client's name
as a reference, and click *Generate link*. The link is unique, works once, and
expires on its own (7 days by default). Send it to the client by email or
WhatsApp.

## 2. The client fills the form

The client opens the link and sees the BCL header and a short note explaining
that their documents are encrypted and used only for the incorporation.

For each designated partner they upload the **PAN card, Aadhaar and latest bank
statement**; for the LLP, the **electricity or telephone bill** for the
registered office.

As each document is uploaded it is read on our server and the related fields
fill themselves in — name, father's name, PAN number, date of birth, present
address, permanent address, office address, name on the bill. Filled fields are
tinted so the client can see what we read and correct anything wrong before
submitting. Whatever no document can supply, they type.

The form refuses data that would be filed wrongly:

- placeholders like "na" or "nil" in a mobile, email or PAN
- a mobile that is not 10 digits, a malformed email, a PAN that is not
  5 letters + 4 digits + 1 letter
- **the same file uploaded for two different partners** — one partner's Aadhaar
  used as another's bank statement would otherwise copy the first partner's
  address onto the second, and it would look perfectly correct on the filing

The link locks itself the moment it is submitted.

## 3. The team downloads one master file

In the dashboard the submission has *Download Excel* and *Download documents*
(a ZIP of every uploaded file). The Excel is the **master file** — four sheets:

| Sheet | What it is |
| --- | --- |
| **Details** | Everything the client submitted, section by section. The single source of truth. |
| **LLP agreement** | Drafting inputs: both parties' names, fathers' names, addresses, capital, the total, each partner's percentage, profit shares. |
| **ODI sheet** | The Indian side filled in; foreign entity, shareholding and bank rows left **yellow** for the team. |
| **Details to be filled** | The sheet the document generator reads. |

The first three sheets are linked by formula. Correct an address on **Details**
and the LLP agreement and ODI sheets update themselves. Green values are pulled
from Details — fix them there, not in place.

**One thing to know:** the fourth sheet, *Details to be filled*, holds fixed
values, not formulas, because the document generator cannot read formulas. If
the team changes something on Details — the final approved LLP name, say — the
same change must be made on *Details to be filled* before generating documents.

## 4. The documents get generated

Upload the same master file to **https://llp-gen.vercel.app**, enter the date
and place, and it produces:

- Consent Letter (Form 9) for each partner
- Subscriber Sheet
- Consent Form under the LLP
- NOC from the property owner

The team fills in the final approved name and anything that changed since
intake, then files.

## What still needs a person

- The **final approved LLP name** — intake collects up to four preferences; MCA
  decides.
- Anything that changed after intake — a new address, a corrected spelling.
- The **ODI foreign-entity and bank rows** (the yellow cells).
- A check that each partner's documents really are their own.

## Where ODI stands

The ODI sheet collects the Indian side automatically. The ODI platform
(`odi_platform`) is a client tracker, and its bank-draft generator currently
receives only three fields — client name, partner and email — so it has nowhere
to put the foreign entity, investment or bank details. Making ODI generate
bank-wise documents from this master file is the next piece of work, and it
needs a decision on whether to extend that platform or generate the ODI
documents the same way the LLP ones are generated.

## The addresses

| What | Where |
| --- | --- |
| Client intake | https://intake.bclworkspace.in |
| Team dashboard | https://intake.bclworkspace.in/admin/login |
| LLP document generator | https://llp-gen.vercel.app |
