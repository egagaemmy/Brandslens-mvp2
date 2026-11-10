"""Four SEO blog posts for the BrandsLens blog, published by /api/setup/seed-blog-seo (idempotent: a post whose slug already exists is skipped).

Written for search intent the first four launch posts do not cover:
  1. media monitoring vs social listening   (comparison / "what is" intent)
  2. how to measure PR results              (how-to, measurement)
  3. which keywords to monitor              (how-to, onboarding)
  4. brand reputation report template       (template / reporting)
Rules these posts follow (tests/test_blog_seo.py enforces most of them): the brand speaks as "we", never as a founder; no invented statistics, customer
stories or quotes; no platform BrandsLens does not scan is named; every claim about the product matches the website; links go only to pages that exist
(www.brandslens.com/product, /pricing, the live demo, and the blog's own posts). Titles and descriptions contain no quotes, angle brackets or ampersands.
"""

SEO_POSTS: list[dict] = [
    {
        "title": "Media Monitoring vs Social Listening Explained",
        "excerpt": "Two terms, one confusing market. What media monitoring and social listening each track, where they overlap, and how to choose what your team needs.",
        "meta_description": "Media monitoring vs social listening: what each one tracks, where they overlap and how to decide which your team needs, or whether you need both.",
        "body_html": """<p>Ask ten people in communications what the difference is between media monitoring and social listening and you will get ten different answers. Vendors do not help: most tools now claim to do both, and the terms are used almost interchangeably on sales pages. The confusion is expensive, because the two disciplines answer different questions, and buying the wrong one means paying for a dashboard that does not help the person who asked for it.</p>
<p>This guide sets out what each one tracks, where they genuinely overlap, and how to decide what your team needs.</p>
<h2>What is media monitoring?</h2>
<p><b>Media monitoring</b> is the practice of tracking what is published about your organisation across news sites, blogs, forums, press releases and other public sources. The goal is to know what is being said about you by outlets and writers whose words carry weight, how it is framed, and whether it needs a response.</p>
<p>It grew out of press clipping services, which cut stories out of newspapers for PR teams. Today it is automated and continuous, but the underlying questions are the same. Who covered us? What did they say? Did they get it right? Is anything here a risk?</p>
<p>The typical users are PR and communications teams, corporate affairs, investor relations and risk and compliance functions: the people accountable for reputation.</p>
<h2>What is social listening?</h2>
<p><b>Social listening</b> is the practice of tracking conversations on social platforms to understand what people think and feel. It covers brand mentions, but also themes, questions, complaints, trends and reactions to campaigns, often from ordinary customers rather than professional writers.</p>
<p>The typical users are social media, marketing, community and customer care teams. They use it to improve campaigns, spot product feedback, find out what an audience cares about and answer people where they are talking.</p>
<h2>The real differences</h2>
<p>The two overlap in technology, but they differ in purpose.</p>
<ul>
<li><b>The question being answered.</b> Media monitoring asks "what is being said about us, and does it put us at risk?" Social listening asks "what does our audience think, want and talk about?"</li>
<li><b>The sources.</b> Media monitoring leans towards published and professionally edited sources, plus forums and open web discussion. Social listening centres on conversations on social platforms.</li>
<li><b>The weight of a single mention.</b> One article in a respected outlet can shape perception for weeks. One social post usually matters because of its pattern and volume, though a single post can also go viral.</li>
<li><b>The output.</b> Media monitoring tends to produce coverage reports, share of voice and incident alerts. Social listening tends to produce audience insight, topic analysis and sentiment over time.</li>
<li><b>The owner.</b> Media monitoring usually belongs to communications and risk. Social listening usually belongs to marketing and social teams.</li>
</ul>
<h2>Where they overlap</h2>
<p>In practice the line blurs, and it should. Both involve finding mentions of a brand, scoring sentiment and comparing against competitors. More importantly, real events do not respect the boundary. A story in the press gets discussed on social platforms within minutes. A viral post gets picked up by journalists within hours. A rumour that starts in a forum thread can end up in a headline.</p>
<p>That is why the most useful way to think about the two is as two views of one conversation. A team that sees only the press will miss the early signal. A team that sees only social platforms will miss the moment a story becomes official.</p>
<h2>Which one does your team need?</h2>
<p>Start with the job to be done, not the label on the tool.</p>
<ul>
<li><b>You are accountable for reputation, coverage or risk.</b> Start with media monitoring, and make sure it also watches forums and social platforms for early warnings.</li>
<li><b>You run campaigns, community or customer care.</b> Start with social listening, and add news coverage so you can see when a conversation becomes a story.</li>
<li><b>You operate in a regulated sector, or in a market where fraud and impersonation are common.</b> Prioritise detection and escalation over analysis. You need a tool that can tell a routine mention from a serious one and get it to the right person.</li>
<li><b>You are a small team.</b> One tool that covers both beats two tools nobody has time to check. Choose on how well it separates signal from noise, not on how many sources it lists.</li>
</ul>
<h2>Three common mix-ups</h2>
<ul>
<li><b>"Social listening is just media monitoring for social platforms."</b> Not quite. The sources overlap, but social listening is built for audience insight at scale, while media monitoring is built for accountability and risk.</li>
<li><b>"More sources means better coverage."</b> A long list of sources means little if the ones your audience uses are thin, or if everything arrives as one undifferentiated stream.</li>
<li><b>"Real-time alerts solve the problem."</b> Speed helps only when the alert reaches someone with the authority to act. A fast alert with no owner is just a faster way to find out nobody was responsible.</li>
</ul>
<h2>Five questions to ask any vendor</h2>
<ol>
<li><b>What exactly do you cover?</b> Ask for the actual list of source types, and how well they cover the places and languages your audience uses. Coverage built around English-language Western media will miss a lot in many markets.</li>
<li><b>Can it tell noise from risk?</b> A feed of ten thousand mentions is not useful. Ask to see how serious mentions are separated from routine ones.</li>
<li><b>What happens after something serious is found?</b> The best detection is wasted if the alert lands in an inbox nobody owns. Look for assignment, escalation and a record of who did what.</li>
<li><b>How does reporting work?</b> The people who read reports usually did not watch the dashboard. Ask what the output looks like for a board or a client.</li>
<li><b>Can you test it on your own brand?</b> A live trial on real keywords tells you more than any demonstration.</li>
</ol>
<h2>How BrandsLens approaches it</h2>
<p>We built <a href="https://www.brandslens.com/product">BrandsLens</a> on the view that this is one conversation. It watches news, forums and social platforms in one place, so the early signal and the official story sit side by side. Each mention it finds is scored by AI for severity and sentiment, so your team reads the serious ones first, and an escalation workflow with an audit trail takes serious incidents from detection to resolution. Share of Voice, coverage value and reports sit on top for the people who need the summary.</p>
<p>If you want to compare tools properly, our guide to <a href="/blog/how-to-choose-the-best-media-monitoring-tool-for-your-brand-in-2026">choosing a media monitoring tool</a> goes deeper on the evaluation questions, and <a href="/blog/from-detection-to-resolution-building-a-real-crisis-escalation-workflow">building a crisis escalation workflow</a> covers what should happen after an alert.</p>
<p>You can <a href="https://app.brandslens.com/demo">see BrandsLens running in a live demo</a> with no sign-up, or look at <a href="https://www.brandslens.com/pricing">pricing</a>.</p>
""",
    },
    {
        "title": "How to Measure PR Results: Metrics That Matter",
        "excerpt": "Most PR reports list activity, not results. The metrics that hold up, how to calculate share of voice, and how to use AVE without being misled.",
        "meta_description": "How to measure PR results with metrics that hold up: share of voice, sentiment, severity, response time, and how to use AVE with care.",
        "body_html": """<p>Most PR reports are really activity reports. They list the press releases sent, the articles published and the number of mentions, and then stop. The people reading them, usually executives or clients, are left asking the question that matters: so what? Did our reputation improve? Are we ahead of our competitors? Did we avoid something that would have cost us?</p>
<p>Good PR measurement answers those questions with a small set of metrics that connect to what the organisation is trying to achieve. This guide covers the ones that hold up, how to calculate them, and how to use the most controversial one, AVE, without being misled by it.</p>
<h2>Start with the question, not the dashboard</h2>
<p>Before choosing metrics, write down what the PR function is meant to achieve this quarter. The usual objectives are building awareness, protecting reputation, reducing risk, supporting a launch, or winning a larger share of the conversation against competitors. Each objective points to different numbers. A launch is judged on coverage and message pick-up. Reputation protection is judged on sentiment and on how quickly problems were handled. If you report every metric your tool offers, nobody will know which ones matter.</p>
<h2>Metric 1: Mention volume, with context</h2>
<p>Volume is the number of times your brand is mentioned in a period. On its own it is a weak metric, because a spike can be good news or bad news. It becomes useful when you add context: compare it with the previous period, mark the events that explain the peaks, and split it by source type so you can see whether growth came from respected outlets or from noise.</p>
<p>Use volume as a starting point for questions, not as a result. "Mentions rose 40 per cent" is a prompt to ask why, not a headline.</p>
<h2>Metric 2: Share of voice</h2>
<p><b>Share of voice</b> measures how much of the conversation in your category belongs to you. The common calculation is:</p>
<p><b>Share of voice = your mentions ÷ (your mentions + your tracked competitors' mentions) × 100</b></p>
<p>Take an illustrative example. In one month your brand has 240 mentions, Competitor A has 360 and Competitor B has 400. Your share of voice is 240 ÷ 1,000, which is 24 per cent. If next month you reach 300 mentions while the competitors stay level, your share rises to 28.3 per cent: 300 ÷ 1,060.</p>
<p>Three rules keep the number honest:</p>
<ul>
<li><b>Fix your competitor set.</b> Changing who you compare against changes the result. Decide the set, keep it for the year and note any change.</li>
<li><b>Compare like with like.</b> Use the same sources and the same period for every brand.</li>
<li><b>Look beyond the count.</b> A hundred mentions in respected outlets is not the same as a hundred in low-quality ones. Read a sample before you draw conclusions.</li>
</ul>
<h2>Metric 3: Sentiment</h2>
<p>Sentiment classifies mentions as positive, neutral or negative, and the useful figure is the trend: the share of each over time. Automated sentiment is a good guide at scale but is never perfect. Sarcasm, mixed language, local expressions, and sentences that praise one thing while criticising another all cause errors. Two habits protect you: report the trend rather than a single score, and read a sample of the underlying mentions each month so that you can vouch for what the numbers say.</p>
<p>Also be clear about what the sentiment is about. A negative mention about a delivery delay and a negative mention about a safety claim are not the same problem, even though both count as negative.</p>
<h2>Metric 4: Severity and risk trend</h2>
<p>Promotion is only half of what PR does. The other half is protection, and a good report shows it. Count the high-severity mentions in each period, such as impersonation, fraud claims, misinformation or regulatory exposure, and show how many were resolved and how. A falling count of serious incidents, or a rising share caught early, is evidence of value that coverage metrics cannot show.</p>
<h2>Metric 5: Time to respond</h2>
<p>For any serious incident, record when it was first published, when you detected it, and when your team first acted. The gap between detection and action is the number you control. Reporting it shows executives that the process works, and it exposes bottlenecks before a real crisis does. Our guide to <a href="/blog/from-detection-to-resolution-building-a-real-crisis-escalation-workflow">building a crisis escalation workflow</a> explains how to make that gap short.</p>
<h2>A word on AVE</h2>
<p><b>Advertising value equivalency</b>, or AVE, estimates what coverage would have cost if it had been bought as advertising. It is popular because it produces a single money figure that executives understand. It is also widely criticised, because editorial coverage and advertising are not the same thing, and the figure says nothing about whether the coverage was positive, accurate or useful. The Barcelona Principles, the measurement framework published by the communications body AMEC, state that AVEs are not the value of communication.</p>
<p>That does not make the figure useless, but it does limit how to use it:</p>
<ul>
<li><b>Call it an estimate.</b> Label it as estimated media value, and say how it was calculated.</li>
<li><b>Never present it as return on investment.</b> It measures the scale of coverage, not business results.</li>
<li><b>Use it next to outcome metrics.</b> Pair it with share of voice, sentiment and risk, so the money figure never stands alone.</li>
</ul>
<p>BrandsLens shows coverage value as an estimate and lets you set the rates it uses, so the figure reflects your own market rather than someone else's assumptions.</p>
<h2>A simple monthly scorecard</h2>
<p>A one-page scorecard is enough for most readers:</p>
<ol>
<li>Headline: one sentence on how the month went.</li>
<li>Mention volume against last month, with the events that explain it.</li>
<li>Share of voice against your fixed competitor set.</li>
<li>Sentiment trend, with two or three representative examples.</li>
<li>Serious incidents: how many, how fast they were handled, what happened.</li>
<li>Estimated coverage value, labelled as an estimate.</li>
<li>What you recommend doing next.</li>
</ol>
<p>Our <a href="/blog/brand-reputation-report-a-simple-template">brand reputation report template</a> turns this into a document you can reuse.</p>
<h2>Common mistakes to avoid</h2>
<ul>
<li><b>Reporting only the wins.</b> A report that never contains bad news will not be trusted when it matters.</li>
<li><b>Changing the method between periods.</b> Comparisons only work when the method stays fixed.</li>
<li><b>Showing too many metrics.</b> Pick five or six that tie to your objectives and explain them.</li>
<li><b>Skipping the reading.</b> Numbers without a sample of the real mentions invite questions you cannot answer.</li>
</ul>
<h2>Where BrandsLens fits</h2>
<p><a href="https://www.brandslens.com/product">BrandsLens</a> tracks mention volume, Share of Voice against the competitors you choose, sentiment, severity and estimated coverage value in one place, and turns them into reports for the people who did not watch the dashboard. You can <a href="https://app.brandslens.com/demo">try the live demo</a> or see <a href="https://www.brandslens.com/pricing">pricing</a>.</p>
""",
    },
    {
        "title": "What Keywords Should You Track for Your Brand?",
        "excerpt": "A monitoring tool only finds what you tell it to look for. A practical checklist of names, spellings, people, products and risk terms.",
        "meta_description": "What keywords should you track for your brand? A practical checklist of names, spellings, products, people and risk terms, with examples.",
        "body_html": """<p>A monitoring tool can only find what you tell it to look for. That sounds obvious, but it is the single biggest reason new accounts report "no mentions". The keywords were either too narrow to match anything, or so broad that they buried the useful mentions in noise. Getting the list right takes about an hour, and it pays back every day.</p>
<p>This guide gives you a practical checklist of what to track, with examples, and how to keep the list sharp.</p>
<h2>How keyword matching works</h2>
<p>Most tools look for the words you give them as written. Two consequences follow.</p>
<ul>
<li><b>Long phrases rarely match.</b> A keyword like "impersonating Sunrise Foods on social media" only matches when someone writes exactly those words in that order. A short, distinctive name such as "Sunrise Foods" catches the same mention and thousands of others.</li>
<li><b>Spelling matters.</b> "Sunrise Foods" and "Sunrize Foods" are different strings. Good tools handle small differences such as spacing and hyphens, but genuine misspellings usually need their own entry.</li>
</ul>
<p>In BrandsLens, for example, spaces and hyphens in a name are treated as the same, so "Sunrise Foods", "Sunrise-Foods" and "SunriseFoods" are caught by one keyword. A true misspelling still needs its own.</p>
<h2>1. Your brand name and its spellings</h2>
<p>Start with the obvious: your company name exactly as people say it. Then add the variations: the short form customers use, the name without "Ltd" or "Limited", an acronym if you have one, and the common misspellings you already see in customer messages. If your name is a common word, pair it with a qualifier, such as your sector or city, or you will drown in unrelated mentions.</p>
<h2>2. Product, service and campaign names</h2>
<p>Customers rarely say the company name. They name the product: the app, the card, the plan, the flagship item. List every product and sub-brand that customers can buy or use, plus the names of active campaigns and any campaign hashtags. These often carry more mentions than the company name itself.</p>
<h2>3. People</h2>
<p>Add the names of your most public people: the chief executive, the spokesperson, and anyone whose statements regularly appear in the press. A quote attributed to your chief executive is a brand mention even when the company is not named. Track people in their professional capacity only, and handle the data in line with the data protection law that applies to you.</p>
<h2>4. Web addresses and handles</h2>
<p>Add your domain name and the usernames you use on social platforms. Links and handles show up in posts that never write your brand name, and they are the raw material for spotting look-alikes. A fake site often copies your domain with one character changed, and a fake account often copies your handle with a number or an extra letter. Our guide to <a href="/blog/brand-impersonation-and-fraud-how-to-detect-and-respond-before-it-costs-you">detecting brand impersonation and fraud</a> covers what to do when you find one.</p>
<h2>5. Risk words: use them carefully</h2>
<p>It is tempting to add every bad word you can think of next to your brand: scam, fake, fraud, refund, complaint, hacked. Resist the urge to write long phrases. Each one only matches an exact wording, so a list full of them matches almost nothing, and in many tools it also uses up your keyword allowance.</p>
<p>A better approach is to track the brand name and let the tool judge seriousness. In BrandsLens, when your brand name is in your list, it catches a post that says "Sunrise Foods scam" without a separate keyword, because each mention it finds is scored for severity. If your tool does not score risk, use short two-word combinations such as your brand with "scam" or "fake", and review them regularly.</p>
<h2>6. Competitors and category terms</h2>
<p>To measure share of voice you need to track your competitors by name. Choose the few that matter most, keep the set fixed, and track each one the same way you track yourself. Add a handful of category terms that describe what you do, such as the product type plus your market, so you notice conversations where nobody has named you yet.</p>
<h2>7. Regulators and industry bodies</h2>
<p>If you work in a regulated sector, add the name of your regulator and the industry bodies that matter to you. Announcements about rules, fines and investigations often appear before they reach your inbox, and they are exactly the news that needs a fast internal response.</p>
<h2>A worked example</h2>
<p>Here is how the checklist looks for a made-up packaged food company called Sunrise Foods, with two products, a managing director and two competitors:</p>
<ul>
<li><b>Brand:</b> "Sunrise Foods" (which also catches "Sunrise-Foods" and "SunriseFoods"), plus the misspelling "Sunrize Foods"</li>
<li><b>Products:</b> "Golden Loaf" and "Crunchy Bites"</li>
<li><b>People:</b> the managing director's full name, and the head of communications if they speak publicly</li>
<li><b>Web addresses and handles:</b> "sunrisefoods.com" and "@sunrisefoods"</li>
<li><b>Competitors:</b> "Harvest Table" and "Daily Grain"</li>
<li><b>Category:</b> "packaged foods Lagos"</li>
<li><b>Regulator:</b> "NAFDAC"</li>
</ul>
<p>That is about a dozen keywords, each short, each distinctive and each tied to a reason. Nothing on the list is a sentence.</p>
<h2>Do you need keywords in other languages?</h2>
<p>If your customers write in more than one language, or mix them, yes. Add the local-language and pidgin names for your products and the terms your customers actually use for what you sell. In many African markets a product has a nickname that appears far more often than its official name, and a keyword list written only in formal English will miss it. Ask your customer care team for the names they hear every day.</p>
<h2>What not to track</h2>
<ul>
<li><b>Single generic words.</b> A keyword such as "energy" or "bank" will match thousands of irrelevant posts.</li>
<li><b>Near-duplicates.</b> If your tool already treats spacing and hyphens as the same, do not add three versions of one name.</li>
<li><b>Everything at once.</b> A focused list of 15 to 25 well-chosen keywords beats 200 loose ones.</li>
</ul>
<h2>A starter checklist</h2>
<ol>
<li>Company name, short form and legal form</li>
<li>Two or three common misspellings</li>
<li>Every product, service and sub-brand</li>
<li>Active campaign names and hashtags</li>
<li>Executives and spokespeople</li>
<li>Domain name and social handles</li>
<li>Two or three competitors</li>
<li>Your regulator and industry body</li>
<li>One or two category terms</li>
</ol>
<h2>How to test your list before you trust it</h2>
<ol>
<li>Search each keyword yourself on the open web and look at the first page. If it is mostly unrelated, the keyword is too broad.</li>
<li>Run the list against past coverage if your tool allows a historical search, and check the results for noise and for mentions you already know about but did not see.</li>
<li>Give the list a week of live results, then read a sample. Tighten what is noisy and add anything the sample shows you missed.</li>
</ol>
<h2>Review the list every month</h2>
<p>Keywords drift. Products launch, campaigns end and competitors appear. Once a month, open the list and ask three questions: which keywords produced nothing, which produced mostly noise, and which new names are missing? Remove the dead ones, tighten the noisy ones and add the new ones. Ten minutes of upkeep keeps the feed trustworthy.</p>
<h2>Where BrandsLens fits</h2>
<p><a href="https://www.brandslens.com/product">BrandsLens</a> is built around this approach: a focused keyword list, spelling variations handled for you, and AI scoring that tells you which mentions are serious. Plans start with 25 keywords and go up to unlimited. See <a href="https://www.brandslens.com/pricing">pricing</a> or <a href="https://app.brandslens.com/demo">try the live demo</a>.</p>
""",
    },
    {
        "title": "Brand Reputation Report: A Simple Template",
        "excerpt": "Most brand reports are dashboards pasted into slides. A six-section template your board will actually read, and how to keep it to two pages.",
        "meta_description": "A simple brand reputation report template: six sections executives read, what to include in each, and how to keep it to two pages.",
        "body_html": """<p>Most brand reports fail for the same reason: they are dashboards pasted into slides. The charts are accurate, the numbers are current, and the reader still closes the file none the wiser. Executives and clients do not want to see everything the tool can measure. They want to know what happened, how the brand is doing, and what they need to decide.</p>
<p>This guide gives you a simple six-section template for a brand reputation report, explains what belongs in each section, and shows how to keep it to two pages.</p>
<h2>Who reads it and what they need</h2>
<p>Write for the person who did not watch the dashboard. That usually means one of three readers.</p>
<ul>
<li><b>The executive or board member</b> who needs to know about risk, trend and decisions in two minutes.</li>
<li><b>The marketing or brand lead</b> who wants to know how campaigns and coverage performed against competitors.</li>
<li><b>The legal, compliance or risk owner</b> who needs to know about serious incidents and what was done about them.</li>
</ul>
<p>One report can serve all three if it is organised so that each can find their section quickly.</p>
<h2>The six-section template</h2>
<h3>1. Headline summary</h3>
<p>Open with three sentences at most: how the period went, the single biggest development, and what needs attention. Add a simple status such as green, amber or red for overall reputation health. Many readers will stop here, so make it complete.</p>
<h3>2. Coverage and share of voice</h3>
<p>Show mention volume against the previous period and your share of voice against a fixed set of competitors. Explain peaks with the events that caused them. One chart and two sentences is usually enough. For the calculation, see our guide to <a href="/blog/how-to-measure-pr-results-metrics-that-matter">measuring PR results</a>.</p>
<h3>3. Sentiment trend</h3>
<p>Show the share of positive, neutral and negative mentions over time, and add two or three representative examples with links to the sources, one positive and one critical. Examples make the numbers believable and show that someone read the mentions.</p>
<h3>4. Incidents and how they were handled</h3>
<p>This is the section that proves the value of monitoring, and the one that matters most to risk and compliance readers. List the serious incidents in the period. For each, give what it was, how severe it was, when it was detected, when your team first acted and how it ended. A short table or list works best. If your process keeps an audit trail, say so: it shows that the response can be demonstrated, not just remembered.</p>
<h3>5. What is coming</h3>
<p>List the events in the next period that could move the numbers: product launches, announcements, regulatory dates, campaigns and competitor activity. This turns the report from a record of the past into a tool for preparation.</p>
<h3>6. Decisions and recommendations</h3>
<p>End with the specific actions you recommend and the decisions you need from the reader. "Approve a holding statement for the upcoming announcement" is a decision. "Continue monitoring" is not. If the report contains no decisions, ask whether the report is needed.</p>
<h2>A copy-and-paste outline</h2>
<ol>
<li>Headline summary: three sentences and a status colour</li>
<li>Coverage and share of voice: volume, comparison with last period, competitors</li>
<li>Sentiment trend: the shares over time, with two or three examples</li>
<li>Incidents: what, how severe, detected, acted on, outcome</li>
<li>What is coming: events that could move the numbers</li>
<li>Decisions and recommendations: what you need from the reader</li>
</ol>
<h2>A worked example of an incident entry</h2>
<p>Here is how one entry might read in the incidents section. The details are made up to show the shape.</p>
<ul>
<li><b>What:</b> A fake account used the company logo to promise a giveaway and collect personal details.</li>
<li><b>Severity:</b> High.</li>
<li><b>First published:</b> Monday evening. <b>Detected:</b> Tuesday at 09:40.</li>
<li><b>First action:</b> Customer notice and platform report sent by 10:30, 50 minutes after detection.</li>
<li><b>Outcome:</b> Account removed on Wednesday, no confirmed customer losses, follow-up check scheduled for next week.</li>
</ul>
<p>Five short lines like this tell a risk owner more than a page of charts, and they build the habit of recording detection and response times.</p>
<h2>Setting a baseline and targets</h2>
<p>A report is only as useful as its comparison. Use three: the previous period, the same period last year if you have it, and your competitors. Once you have two or three periods of data, set simple targets, such as a share of voice you want to reach, a share of positive and neutral sentiment you want to stay above, or a maximum time to first action on serious incidents. Targets turn numbers into a conversation about performance.</p>
<h2>Adapting the report for different audiences</h2>
<ul>
<li><b>For a board:</b> Keep to one page, lead with status and decisions, and move everything else to an appendix.</li>
<li><b>For a client, if you are an agency:</b> Add the activity you carried out and tie each result back to the agreed objectives, so the report shows the value of the work as well as the numbers.</li>
<li><b>For a leadership meeting:</b> Use the same sections, but present the incidents and the recommendations live and keep the charts for reference.</li>
</ul>
<h2>How to keep it to two pages</h2>
<ul>
<li><b>Lead with the answer.</b> Put conclusions first and detail after.</li>
<li><b>Compare, always.</b> A number without last period or a competitor beside it means little.</li>
<li><b>Use one chart per section.</b> More than that, and nothing stands out.</li>
<li><b>Write in plain language.</b> Replace tool terms with what they mean for the business.</li>
<li><b>Move detail to an appendix.</b> Link to the full data for the few readers who want it.</li>
</ul>
<h2>How often to report</h2>
<p>Match the rhythm to the reader. A monthly report suits executives and boards. A weekly summary suits the communications team. Serious incidents should get their own short note as they happen, rather than waiting for the monthly cycle. Our guide to <a href="/blog/from-detection-to-resolution-building-a-real-crisis-escalation-workflow">crisis escalation workflows</a> explains how to run that process.</p>
<h2>Mistakes that make reports ignored</h2>
<ul>
<li><b>Only reporting wins.</b> If bad news never appears, readers stop trusting the good news.</li>
<li><b>Unexplained spikes.</b> A chart with a peak and no explanation raises questions you will have to answer live.</li>
<li><b>No baseline.</b> Without a comparison, the reader cannot tell whether a number is good.</li>
<li><b>Jargon.</b> Terms like "sentiment polarity" lose a board member in the first paragraph.</li>
<li><b>No evidence.</b> Claims without links to the real mentions invite doubt.</li>
</ul>
<h2>Producing the report faster</h2>
<p>The template is simple. The work is gathering the numbers, writing the summary and formatting the output. <a href="https://www.brandslens.com/product">BrandsLens</a> does the gathering: volume, Share of Voice, sentiment, severity and the full incident record sit in one place, and reports export to Excel, branded PDF and native PowerPoint, depending on your plan, so the output is ready to send to people who never opened the dashboard. See <a href="https://www.brandslens.com/pricing">pricing</a> for which formats each plan includes, or <a href="https://app.brandslens.com/demo">try the live demo</a>.</p>
""",
    },
]
