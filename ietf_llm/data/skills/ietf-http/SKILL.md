---
name: ietf-http
description: Best practices for writing IETF specifications that use or extend HTTP, per BCP 56 / RFC 9205. Use whenever an Internet-Draft, RFC, or similar specification defines a protocol or API over HTTP -- reviewing one, writing one, or revising one -- including when the user asks about only a piece of it, such as defining a header field, status code, method, media type, URL structure, caching behaviour, or an API's error format. Trigger it even when the user never says BCP 56, HTTP review, or httpbis; asks like 'does this draft use HTTP right?', 'I'm adding a header to my draft', or 'review my API spec' are all in scope. Not for implementing HTTP clients or servers, or debugging live traffic.
license: CC-BY-4.0
---

# HTTP Specification Skill

Drafting, reviewing, or modifying IETF Internet-Drafts and similar specifications that use the HTTP protocol. Covers editorial style, best practices, and common pitfalls. The content of this skill mirrors much of the content of BCP 56 / RFC 9205.

This skill is designed to give initial advice to draft authors. It is not definitive; there are aspects of HTTP design that require expert advice and intervention. Draft authors are encouraged to run it and consider the provided advice. If they disagree with the recommendations, have further questions, or need further advice, encourage them to engage with HTTP experts (e.g., the HTTP Directorate, for IETF specifications).

Many of its recommendations are just that -- their application depends upon the specific circumstances and goals of the use of HTTP in question. When a particular behaviour is required or prohibited in IETF specifications, MUST / MUST NOT will be used.

To use this skill:

First, confirm that HTTP is being used by the specification in question; see "Is HTTP Being Used?" If it is not, stop and advise the user that this skill isn't applicable. 

From there:
* If you are creating or modifying a specification that uses HTTP, follow the steps in "Creating HTTP Specifications".
* If you are reviewing a specification that uses HTTP, follow the steps in "Review Process". That covers the HTTP substance; for how to conduct the review itself — which question it answers at this document's stage, how to weigh what you find, and how to present it — see the `ietf-reviewing` skill.


## Reference Texts

The full text of the core specifications is bundled with this skill, in `reference/`:

| File | Document |
| --- | --- |
| `reference/rfc9205.txt` | BCP 56 -- Building Protocols with HTTP |
| `reference/rfc9110.txt` | RFC 9110 -- HTTP Semantics |
| `reference/rfc9111.txt` | RFC 9111 -- HTTP Caching |
| `reference/rfc8820.txt` | BCP 190 -- URI Design and Ownership |

Consult them whenever precision matters: quoting normative text, confirming a section number before citing it, or checking what a protocol element actually requires. Don't paraphrase these documents from memory.

Where this skill and these documents appear to diverge, work out which is the case before acting. This skill sometimes says more than BCP 56 does, or reflects later developments (e.g. the QUERY method, RFC 9651 superseding RFC 8941); but on anything RFC 9110, RFC 9111, or BCP 190 states normatively, those documents govern.

These files are large -- RFC 9110 is over 10,000 lines -- so navigate rather than read them end to end. Each has a table of contents near the top, and section headings appear at the start of a line as the number followed by two spaces and the title (`9.3.1.  GET`), so grepping for `^9\.3\.1\.` finds a section's line number to read from.

## Is HTTP Being Used?

A specification is using HTTP -- and so this skill applies -- if it:

* uses transport port 80 or 443, or
* uses the "http" or "https" URI scheme, or
* uses an ALPN protocol ID that generically identifies HTTP (e.g., "http/1.1", "h2", "h3"), or
* makes registrations in, or overall modifications to, the IANA registries defined for HTTP.

When a specification is using HTTP, all of the requirements of the HTTP protocol suite are in force -- RFC 9110 in particular, but also the specific version(s) of HTTP in use and any extensions.

An application can rely on parts of the HTTP specification without meeting these criteria -- for example, reusing its message format for other purposes. Doing so buys freedom to modify the protocol, at the cost of most of the benefits in "Goals for Using HTTP", because existing implementations won't easily adapt. Such specifications MUST NOT use HTTP's URI schemes, transport ports, ALPN protocol IDs, or IANA registries; they are encouraged to establish their own.

## Creating HTTP Specifications

Follow these steps when the user wishes to create a new specification that uses HTTP.

### 0. Understand the Relationship with HTTP

Talk to the user about why they are using HTTP for their protocol. While most authors will have broad familiarity with the protocol, they may not appreciate everything it provides (see in particular "Goals for Using HTTP"), or all of the constraints that will be placed upon them (see "Web Compatibility"). A discussion about these aspects with the user will assure that HTTP is a good fit for their application.

### 1. Define Your Resources and Representations

Start by specifying the application's server-side behaviour by defining these protocol elements:

* Formats, identified by media types and often based on a format convention such as JSON (see "Content");
* HTTP fields, per "Header and Trailer Fields"; and
* Resources that consume and produce those formats when operated on by methods (see "Methods"), returning responses associated with status codes (see "Status Codes") and whose types are identified by link relations (see "Linking").

Between them, these primitives can express retrieval of resource state with GET, creation or update with POST or PUT, data processing with POST, and deletion with DELETE -- without the specification fixing any URL. For example:

> Resources linked to with the "example-widget" link relation type are Widgets. The state of a Widget can be fetched in the "application/example-widget+json" format, and can be updated by PUT to the same link. Widget resources can be deleted.

Generally, a protocol that uses HTTP well will define a number of specific formats and resource types. Those that only define a single format or resource type may be at risk of tunnelling (see "Tunnelling").

### 2. Define Client Constraints

Next, define the requirements and assumptions for clients accessing those resources. 

Generally, client behaviour ought to be closely aligned with that of Web browsers, to avoid interoperability problems when browsers are used -- even if they are not the intended client population, it is almost inevitable that browsers will at some point access the resources defined by an application. 

Some client behaviours and extensions aren't required by HTTP but have become common; if the specification doesn't address them explicitly, expect confusion:

* Redirects: specify how they are to be handled (see "Redirection").
* Cookies: reference the Cookie specification explicitly if they are required.
* Certificates: specify that TLS certificates are checked per Section 4.3.4 of HTTP when HTTPS is used.


## Review Process

To review a specification that uses HTTP, go through the following steps. 

1. Identify the section(s) of the specification that use or extend HTTP.
2. Evaluate them according to the criteria in this skill.
3. Double check the document's references for currency and correctness (see "References").
4. Produce findings per the `ietf-reviewing` skill. The template below is for the review text, if one is asked for.

If the specification is a revision, check for prior HTTP reviews (from this reviewer or others, e.g. HTTPDIR) and reconcile against them: note which points were addressed and which recur unchanged.

### Review Template

Use this when writing the review itself -- see `ietf-reviewing` for when that happens. Empty sections should be omitted, and if other kinds of information are relevant, new sections should be created as appropriate.

~~~markdown
# HTTP Review for [document identifier]

## Overview

_assess, at a high level, how likely the specification is to benefit from using HTTP; see "Goals for Using HTTP"_

_assess, at a high level, how web-compatible the specification is; see "Web Compatibility"_

## Technical Recommendations

_For each relevant skill section, create a `###` subsection and evaluate the specification's conformance, making concrete recommendations. Omit sections that don't apply. Where it aids the authors, separate factual defects that have a single correct answer (state the correction) from design choices that need an author decision (pose these as questions rather than diffs) -- the former are harder to leave unaddressed when they can't be reframed as matters of taste._

## Editorial Nits

_Review "Editorial Style" and enumerate the issues found, as a numbered list with appropriate references. Keep this as compact as possible; for example, do not emit five separate 'needs an example in this format' nits; instead, emit one with five sub-bullets or similar references._
~~~


## Goals for Using HTTP

When deciding to use HTTP, as well as how to use the protocol, it is helpful to have a solid grasp of the benefits of doing so. 

The primary benefit of using HTTP as the basis of a protocol is _reuse_ -- of deployed infrastructure, available implementations and tools, and knowledge / familiarity by humans. HTTP offers intuitive, higher-level abstractions that -- when properly used -- can handle problems without significant effort. 

For example, it can provide:

* Message framing
* Well-understood semantics (e.g. "GET")
* Caching for scalability, reliability, and performance
* Intermediary infrastructure for management and operability
* Security infrastructure such as WAFs
* Authentication frameworks
* Browsers as a powerful user interface
* Protocol evolution mechanisms
* Flexibility in deployment (e.g., alongside other Web applications, and integrated into them)
* Separation of interface and implementation 

Often, protocol designers may not appreciate or anticipate these benefits immediately. For example, it may not be apparent that caching is necessary in the early deployments of a protocol, but at scale it is often a critical capability. Designing to enable use of as many of HTTP's capabilities as is reasonably possible enables these unforeseen future requirements to be met more easily.


## Web Compatibility

The Web is a rich, interlinked ecosystem with many actors. Applications that use HTTP need to co-exist well with other parts of this ecosystem, even when there is no intended overlap. 

This has many implications:

* Because the most widespread client for HTTP is the Web browser, applications MUST consider the implications of browser access even if it is not a target client; see "Security" for details. 
* Infrastructure deployed for HTTP (such as CDNs, WAFs, caches, and servers) often have no application-specific knowledge, and are difficult to modify. Therefore, applications need to be designed with it in mind; see especially "Caching", "Stateful Connections", and "Subsuming Generic Semantics".
* Some applications can benefit from flexible deployment on servers, "mixing and matching" with other applications, as well as cross-linking between them. See "Linking".

### HTTP Versions

Specifications MUST NOT require a minimum version of HTTP; because HTTP is hop-by-hop, connections can be handled by implementations the application doesn't control (proxies, CDNs, firewalls). If there are particular benefits from a particular version (e.g., HTTP/2's multiplexing), note that instead. Likewise, specifications MUST NOT specify a maximum version, to preserve the protocol's ability to evolve.

HTTP/2 and HTTP/3 define server push.  Use of this feature is generally inadvisable; it is not widely available.

## Resources and URIs

Resources are identified by URIs (also called URLs, but most IETF specifications use URI). They consume and produce representations -- bundles of content with header (and possibly trailer) fields. Requests contain a method that operate upon the resource; responses contain a status code that indicates the outcome of the operation. 

Applications will typically use the "http" and/or "https" URI schemes; "https" is strongly RECOMMENDED, to provide authentication, integrity, and confidentiality and to mitigate pervasive monitoring. An application-specific scheme can be defined, but the trade-offs are severe: browsers and existing clients, intermediaries, and servers won't recognise it; URLs are often generated automatically, so consistent use is hard to guarantee; the resources remain available over "http" and/or "https" anyway, so those URLs can leak; and web-related features (same-origin policy, cookies, authentication, caching, HSTS, CORS, secure contexts) may not work as expected, because they generally assume the scheme is "http" or "https".

Applications can use the default port (80 for HTTP, 443 for HTTPS) or be deployed on another; this is usually a deployment-time decision. A non-default port has to be reflected in the authority of every URL for the resource -- the only way to change a default port is to change the URI scheme. Using a non-default port has privacy implications (the protocol can now be distinguished from other traffic) that should be documented in Security Considerations, as well as operability concerns, since some networks might block or interfere with it.

### Linking

Assuming that a server's namespace (or a portion of it) is exclusively for one application's use overlays application-specific semantics onto that space and precludes others from using it. Such "squatting" usurps the server's authority over its own resources and is bad practice in standards.

Specifications that use HTTP MUST NOT specify fixed paths for their resources. For example, specifying that `/app/widget.xml` is the path for a particular resources in the protocol is not allowed. See [BCP 190 / RFC 8820](https://www.rfc-editor.org/rfc/rfc8820) for details.

The one exception to this is a `/.well-known` URI - see "Discovery".

Instead of statically defining URI paths, it is RECOMMENDED that applications define and use typed links. This can be done by adding URIs with specific semantics to application-specific representation formats or by using the generically-typed linked relations defined in [RFC 8288](https://www.rfc-editor.org/info/rfc8288/). Doing so has significant operational advantages: servers can arrange their resources with more flexibility, link between applications more easily, and redirect requests to different servers. Linking also offers a natural mechanism for extensibility and capability management (since the document carrying the links can also describe their targets), and provide a form of cache invalidation -- when a resource's state changes, change the affected links so that a fresh copy is fetched.

Applications can also use [URI Templates](https://www.rfc-editor.org/info/rfc6570/) to let clients generate URLs from runtime data.

A resource type can specify the semantics and syntax of the query components of its URIs. While many use the `foo=bar&baz=bam` format from HTML forms due to its widespread use, this is not required. 

### Discovery

A client typically begins interacting with a server by fetching an initial document that describes the particular deployment and links to other relevant resources. This keeps the deployment flexible (potentially spanning multiple servers), allows evolution, and lets the application tailor the discovery document to the client. Once fetched, it can be cached (if its metadata allows) and used to locate the application's other resources.

The most straightforward way for a client to find that initial discovery document is to be configured with its full URL. If the client only knows the server's hostname and the identity of the application, a specification can instead:

* register a [well-known URI](https://www.rfc-editor.org/info/rfc8615/) as an entry point, giving a fixed path on every potential server that won't collide with other applications; or
* enable the server authority to convey a URI Template (or similar) for generating an entry point URL -- for example, in a configuration document.

An application that doesn't want a discovery document -- because communication is very brief, or the added latency is unacceptable -- can place all of its resources under a well-known location.

### Redirection

The 3xx status codes direct the client to another resource. The most common -- 301, 302, 307, and 308 -- all use the Location response header field, and differ along two axes:

| | Permanent | Temporary |
| --- | --- | --- |
| Allows change of the request method from POST to GET | 301 | 302 |
| Does not allow change of the request method | 308 | 307 |

Permanent redirects can be used to update links stored in the client (e.g., bookmarks); temporary ones cannot. This is separate from HTTP caching and has no effect on it. Browsers generally do change POST to GET for 301 and 302, which is why 307 and 308 were created. 303 (See Other) informs the client that the result of an operation is available at a different location using GET.

If an application needs redirects to be followed automatically, it MUST say so, specifying the circumstances.

Redirects can be cached when appropriate cache directives are present, but they aren't "sticky": redirecting one URI doesn't imply that similar URIs (e.g., with different query parameters) are redirected.

Applications are encouraged to specify that 301 and 302 change the subsequent request method from POST (but no other method) to GET, for compatibility with browsers. A redirected request's header fields are generally copied from the original, but some change -- e.g., Authorization and Cookie will change if the origin (and sometimes path) changes. If a request header field the application defines needs to be modified or removed on redirect, say so -- but don't rely on it, since generic clients won't be aware of the requirement.

## Content

Common syntactic conventions for message content include JSON, XML, and CBOR; best practices for their use are out of scope here.

Applications should register a distinct media type for each format they define, so that it can be identified unambiguously and negotiated for.

## Methods

Protocols that use HTTP must only use methods that are registered in the IANA HTTP method registry, and will typically confine themselves to the following methods: GET POST PUT DELETE PATCH QUERY

See below for advice about using specific methods.

### GET

GET is the most common and useful method; its retrieval semantics allow caching and side-effect-free linking, and underlie many of the benefits of using HTTP.

Queries can be performed with GET, usually using the query component of the URL -- a familiar pattern from Web browsing, with the advantage that results can be cached. GET can be unwieldy for larger queries, though: binary query terms have to be encoded to conform to URI syntax, and some implementations limit URL size (modern software typically allows considerably more than the 8000 octets required by HTTP). For these cases, see QUERY.

Processing of a GET should not change application state or have other side effects significant to the client, since implementations do retry failed GETs, and GETs protected by TLS early data may be vulnerable to replay. Side effects do not include logging and similar functions.

A particular thing to watch for: a GET whose purpose is to create server state -- minting a session, token, or resource. Because GET is safe, such a request can be prefetched, retried by intermediaries, or replayed from TLS early data; the creating request should use POST.

### POST

POST is the appropriate method for data processing, and for resource creation or update where the client isn't supplying the target URL. See also "GET" (for queries) and "Tunnelling" (for overuse of POST).

Because a browser can be coaxed into cross-site request forgery (CSRF) from an arbitrary site, an application whose state can be changed with POST needs to consider browsers even if it doesn't intend to be used by one; see "Security".

### PATCH

PATCH allows a client to deterministically update the state of a resource using a patch format. When specifying use of PATCH, the specific patching algorithm MUST be determined by the media type of the request; resources MUST NOT assume a patch type without this information.

### QUERY

QUERY is a relatively new method that was designed to address limitations of expressing complex queries using GET (mostly due to URL length limitations). Applications that use QUERY should understand that caching support for it is not yet widespread, and that some software (e.g., WAFs) might block its use until it becomes more widespread.

### OPTIONS

OPTIONS is often considered for retrieving metadata, because HTTP-based APIs frequently need it, but it has significant limitations:

* Metadata can't be linked to with a simple URL, because OPTIONS is not the default method.
* OPTIONS responses aren't cacheable; HTTP caches operate on representations (i.e., GET and HEAD). Caching them separately requires considering interactions with cache expiry, secondary keys, and other mechanisms.
* It is "chatty" -- retrieving metadata separately increases the number of requests.
* Implementation support isn't universal; some servers can't easily respond to OPTIONS at all.

Usually one of these is a better fit:

* For server-wide metadata, create a well-known URI, or use an existing one if appropriate.
* For metadata about a specific resource, create a separate resource and link to it with a Link response header field or a link in the response's content. The Link header field is available on HEAD responses, which is useful when a client wants to discover a resource's capabilities before interacting with it.

### Defining New Methods

Extending HTTP with new methods is rare. New methods require IETF Review for registration, and MUST be generic -- potentially applicable to all resources, not just those of one application. Some applications have historically defined application-specific methods; HTTP now forbids this.

When authors believe a new method is required, they are encouraged to engage with the HTTP community early (e.g., on the ietf-http-wg@w3.org mailing list) and to document the proposal as a separate HTTP extension, not as part of an application's specification.

## Status Codes

Status codes are generic; they convey semantics both to generic HTTP components -- caches, intermediaries, clients -- and to applications. Applications encounter two recurring pitfalls in using them.

First, status codes are often generated by something other than the application: by network errors, captive portals, proxies, CDNs, an overloaded server, or generic client software. If an application assigns specific semantics to such a status code, a client can be misled about its state.

Second, mapping application errors one-to-one onto status codes exhausts the finite space of applicable codes, which in turn encourages minting application-specific codes or reusing existing ones whose semantics barely fit.

Therefore, applications should:

* Use the most applicable status code, making generous use of the general codes (200, 400, and 500) when in doubt.
* Not specify a one-to-one relationship between status codes and application errors.
* Convey finer-grained error information in the response's content and/or header fields; [Problem Details](https://www.rfc-editor.org/rfc/rfc9457) is one way to do so.
* Point out explicitly that clients ought to handle all applicable status codes gracefully, falling back to the generic n00 semantics of a code they don't recognise (e.g., handling 499 as 400). This is preferable to a "laundry list" of potential status codes, which won't stay complete.

Applications MUST only use registered status codes, and MUST NOT re-specify their semantics -- even by copying their definition. It is NOT RECOMMENDED that specific reason phrases be required; the reason phrase has no function in HTTP, isn't guaranteed to be preserved, and isn't carried at all in HTTP/2 or HTTP/3.

### Defining New Status Codes

Extending HTTP with new status codes is rare, and requires a new IETF consensus specification. Most often, the HTTP Working Group (httpbis) is the appropriate venue for their discussion, although other Working Groups that are chartered to work on specific HTTP extensions might too.

Specifications that propose a new status code MUST NOT "squat" on a code by choosing their own number; this will be done later in the Working Group process. Before that happens, always refer to the proposed status code using its top-level category number followed by two placeholder characters; e.g., "4xx" or "5nn".

Status codes are generic; they MUST NOT be specific to a particular application or domain of use. They MUST be potentially applicable to all HTTP resources. See [Considerations for New Status Codes](https://httpwg.org/specs/rfc9110.html#considerations.for.new.status.codes).

Status codes MUST be defined in standalone documents. Experience shows that when they are defined as part of an application or domain specific protocol, readers will assume that they are only to be used in that circumstance.

## Header and Trailer Fields

Defining a new field is appropriate when at least one of the following holds:

* the field is useful to intermediaries, who often wish to avoid parsing message content;
* the field is useful to generic HTTP software (e.g., clients, servers); or
* the value can't be carried in the message content, usually because the format doesn't allow it.

Otherwise, it is usually better to convey application-specific information elsewhere -- e.g., in the message content, or the URL's query string.

The semantics of existing fields MUST NOT be redefined without updating their registration or defining an extension to them (where that is allowed). For example, an application cannot specify that the Location header field has a special meaning in a certain context.

Request header fields used to select ("vary") a response have consequences for caching, and need careful consideration; see "Caching". Fields that carry application state (e.g., Cookie) have their own considerations; see "Security".

### Defining New Fields

New fields MUST be registered.

HTTP fields defined in the IETF should be [Structured Fields](https://httpwg.org/specs/rfc9651.html). This isn’t an official policy, but many participants will argue that their interoperability and security benefits override most other concerns, so failing to use them may cause issues during Last Call.

Field names should be short -- there is overhead even when field compression is used -- but appropriately specific. A field that is specific to an application can use an identifier for that application as a prefix, separated by a hyphen; e.g., the "example" application might define "example-foo", "example-bar", and "example-baz". The motivation is to avoid consuming more generic field names, not to reserve a portion of the namespace for the application.

## Caching

Caching is one of the primary benefits of using HTTP: it provides scalability, reduces latency, and improves reliability, and caches are readily available in browsers and other clients, in networks as forward and reverse proxies, in CDNs, and in server software. Even an application that isn't designed to take advantage of caching needs to consider how caches will handle its responses, so that behaviour stays correct when one is interposed.

Assigning even a short freshness lifetime -- e.g., 5 seconds -- allows a response to be reused across multiple clients, or by one client repeating a request. In general, if it is safe to reuse something, consider assigning a freshness lifetime. Notes on doing so:

* The most common method is the max-age response directive. Expires also works, but isn't necessary; all modern caches support Cache-Control, and a delta is more convenient and less error-prone.
* The public response directive is not needed for most responses; it is only necessary to store an authenticated response, or when the status code isn't understood by the cache and there is no explicit freshness information.
* Responses without explicit freshness directives may be stored and served using a heuristic freshness lifetime. The heuristic isn't under the application's control, so it is generally preferable to set an explicit lifetime or make the response explicitly uncacheable.
* To prevent storage, the directive is no-store, and no other is needed. A frequent mistake is using no-cache or Expires: 0 to try to stop caching: no-cache permits storage and only forces revalidation, and Expires: 0 merely sets an already-past expiry (relying on an invalid date being treated as stale). Neither prevents a cache from storing the response, which matters when it carries per-client data such as a session identifier.

Stale responses (e.g., with `Cache-Control: max-age=0`) can be reused by caches when disconnected from the origin server, which can be useful for handling network issues. If that isn't suitable for a given response, send the must-revalidate directive. Assigning a validator lets stale responses be refreshed, saving both bandwidth and latency for large responses.

When an application needs to express a lifetime separate from the freshness lifetime, convey it separately -- in the response's content, or a separate header field -- and consider the relationship between the two carefully, since the response will be used for as long as it is considered fresh. In particular, consider how responses that aren't freshly obtained from the origin should be handled: a concept like a validity period will need to account for the age of the response. One way to address this is to specify explicitly that responses need to be fresh upon use.

If a request header field changes the response's header fields or content, point out that this has caching implications: such resources need either to make their responses uncacheable, or to send Vary on all responses from that resource -- including the "default" response.

## Security

Even if an application isn't intended to be used with a Web browser, its resources remain available to browsers and other HTTP clients, so every application that uses HTTP needs to consider how browsers will interact with it. If application state can be changed with POST, a browser can easily be coaxed into cross-site request forgery (CSRF) from an arbitrary site. If an attacker gains control of content returned by the application's resources -- for example, part of the request is reflected in the response -- they can inject code into the browser and access data and capabilities as if they were the origin, a cross-site scripting (XSS) attack.

The best approach is to consider the application as a Web application and follow best practices for secure development of one. A complete enumeration is out of scope, but some considerations:

* Use an application-specific media type in Content-Type, and require clients to fail if it isn't used.
* Use `X-Content-Type-Options: nosniff` so that attacker-controlled content can't be coaxed into a form a browser interprets as active content.
* Use Content-Security-Policy to constrain the capabilities of active content, mitigating XSS.
* Use Referrer-Policy to prevent sensitive data in URLs leaking in the Referer request header field.
* Use the 'HttpOnly' flag on cookies so that they aren't exposed to browser scripting languages.
* Avoid compressing sensitive information (e.g., authentication tokens, passwords); the scripting environment browsers offer allows an attacker to repeatedly probe the compression space, and an attacker on the network path can use that to recover the information.

For example, a response from an application that doesn't intend its content to be treated as active:

~~~ http-message
HTTP/1.1 200 OK
Content-Type: application/example+json
X-Content-Type-Options: nosniff
Content-Security-Policy: default-src 'none'
Cache-Control: max-age=3600
Referrer-Policy: no-referrer

[ content ]
~~~

Depending on the intended deployment, a specification might require these mechanisms in specific ways, or merely point them out in Security Considerations.

Applications can use cookies to identify a client or store client-specific data, and HTTP authentication to identify clients. In both cases, specify their scoping and use carefully: if the application exposes sensitive data or capabilities -- e.g., by acting as an ambient authority -- exploits are possible. Mitigations include using a request-specific token to ensure the intent of the client. Note that Basic and Digest are not suitable unless the channel is secure, and that TLS client certificate authentication is scoped to the underlying transport connection, so a client has no way of knowing whether the authenticated status was used in preparing a response.

Because many HTTP capabilities are scoped to the origin, consider how a deployment might interact with other applications -- including Web browsing -- on the same origin server. Cookies, for example, are sent with all requests to the origin by default unless scoped by path, so the application might receive cookies from other applications, leading to security issues and name collisions. Requiring a dedicated hostname solves this, but it is often desirable to allow multiple applications on one hostname, since that gives the most deployment flexibility. Applications should therefore strive to allow multiple applications on an origin: when specifying the use of cookies, HTTP authentication realms, or other origin-wide mechanisms, don't mandate a particular name -- let deployments configure it, and consider scoping to part of the origin using the mechanisms specified for doing so. Applications that wish to expose cross-origin data to browsers will need to implement CORS.

Privacy considerations deserve their own attention. Beyond what is sent explicitly and what is visible on the wire (one of the reasons "https" is recommended), information can be gathered by connecting a user's activities over time:

* Session information -- the client's IP address, TLS session tickets, cookies, ETags stored in the client's cache, and other stateful mechanisms. Avoid session mechanisms unless they are unavoidable or necessary for operation; when they are used, document the risks and encourage implementations to allow clearing the state.
* Fingerprinting -- unique aspects of a client's messages and behaviour, such as User-Agent and Accept-Language, which in combination can uniquely identify a client. Specify that clients should only emit the information they need to function.
* Code execution -- if the application exposes the ability to execute code, any ability to observe the environment can be used both to fingerprint the client and to obtain and manipulate private data. For example, access to high-resolution timers can profile the underlying hardware. Avoid mobile code where possible; where it can't be avoided, scrutinise the resulting security properties carefully.

Finally, note that applications that require modification of implementations -- a new URI scheme, a non-standard method -- risk having those implementations "fork" from their parent HTTP implementations, and so not benefit from security improvements made upstream.

## Anti-Patterns

The following anti-patterns should be called out and warned against (with explanation) when encountered:

### Tunnelling

"Tunnelling" is treating HTTP as a transport protocol. Typically, this is seen as overuse of POST -- in extreme cases, putting all traffic through POSTs to a single resource. Sometimes, tunnelling is indicated when a protocol creates a "HTTP binding" to existing semantics, since those semantics rarely map directly onto HTTP resources and representations.

Tunnelling indicates that the application is unlikely to realise most of the benefits of using HTTP (see "Goals for Using HTTP"). 

Tunnelling is not forbidden in specifications, but careful attention needs to be paid to how HTTP is used, because authors that do not engage deeply with the protocol often make invalid assumptions about how the protocol is deployed -- for example, assuming that connections are stateful.

### Stateful Connections

HTTP's use of the underlying transport protocol is stateless. Just because two messages occurred on the same connection does not guarantee that they will remain on the same connection when forwarded to a downstream recipient.

Therefore, specifications MUST NOT assume that two messages have any non-explicit relationship. If statefulness is required, consider using HTTP Cookies or other mechanisms. 

Cookies re-establish the association between messages, but a cookie only identifies a session -- it does not route the request to a server holding that session's state. "Sticky" routing can pin a client to one backend, but only across infrastructure the operator controls; an intermediary they don't control (a CDN, a forward proxy) may spread a client's requests across backends anyway. So a specification whose correctness depends on session state MUST NOT treat connection affinity and shared session storage as interchangeable -- only shared storage stays correct when requests are spread across servers.

### GET with Content

The generic HTTP syntax allows a GET request to contain content, but only so that message parsers can be generic. Content in a GET is not recommended, has no meaning, and will be ignored or rejected by generic HTTP software -- intermediaries, caches, servers, and client libraries. Use the query component of the URI, POST, or QUERY; see "GET".

Specifications MUST NOT use GET with content.

### Mandatory Features

Applications MUST NOT require clients to statically support HTTP features that are usually negotiated. For example, requiring support for responses with a certain content coding, instead of negotiating for it, means that otherwise conformant clients cannot interoperate with the application. Encouraging implementation of such features is fine.

Similarly, don't require a minimum or maximum version of HTTP; see "HTTP Versions".

### Subsuming Generic Semantics

Much of the value of HTTP is in its generic semantics: the protocol elements HTTP defines are potentially applicable to every resource, and aren't specific to a particular context. That split lets a message be handled by common software -- servers, intermediaries, client implementations, caches -- without those implementations understanding the application, and lets people apply their knowledge of HTTP without specialised knowledge of the application.

Therefore, specifications MUST NOT redefine, refine, or overlay the semantics of generic protocol elements such as methods, status codes, or existing header fields. Application-specific semantics belong in message content and in fields the application defines -- and specifications should focus on the protocol elements that are specific to the application, namely its resources.

A related trap is specifying exactly how HTTP is to be implemented and used, which easily produces an unintended profile of HTTP behaviour. For example:

> A POST request MUST result in a 201 (Created) response.

This forms an expectation in the client that the response will always be 201, when in fact a real deployment might return something else -- a proxy requiring authentication, a server-side error, a redirect. If the client doesn't anticipate that, the deployment is brittle.


## Editorial Style

The following guidelines apply to IETF specifications that define, extend, or use HTTP.

### Header and Trailer Fields

When defining a field, the first instance should be quoted; e.g.,

> This document defines the "Foo" response header field.

If the field is specific to headers, trailers, requests, and/or responses, the definition should include the relevant terms, as above.

When referring to a field defined in a different document, the first instance should include a reference, and all instances should be unquoted. For example:

> Add the Foo-Example header field (see {{RFCxxxx}}) to the response.

Subsequent occurrences should be unquoted, but always be followed by “field”, “header field”, or “trailer field” as appropriate.

See also [Considerations for New Fields](https://httpwg.org/specs/rfc9110.html#considerations.for.new.fields).

### Structured Fields

When specifying a Structured Field in prose, preferred practice is to add the following to your “Notational Conventions” section:

> This document uses the following terminology from {{Section 3 of STRUCTURED-FIELDS}}
> to specify syntax and parsing: List, Dictionary, and Integer.

adjusting the terms listed as appropriate. Then, when using one of the terms, just use the bare, capitalised term; e.g.,

> The Foo header field’s value is a List of Integers.

All references to structured types should be made to Section 3 of the Structured Fields specification, not Section 4.

Although ABNF is defined for structured types, we do not recommend its use. The current reference for Structured Fields is RFC 9651, not RFC 8941.

### Content

Use ‘content’, not ‘body’ or ‘payload’.

### Methods

Just use the bare method name (without quotes or emphasis); e.g.,

> Send a request with the GET method.

Stating that it is a HTTP method is optional; this is equally acceptable:

> Send a GET request.

### Status Codes

Use the bare status code number, followed by the reason phrase in parentheses. For example:

> The 500 (Internal Server Error) status code.

When referring to multiple individual status codes, the reason phrase can be omitted; for example:

> If the status code is 200, 202, or 204, proceed.

To refer to a range of status codes, use “xx” notation:

> The 4xx range of status codes.

When discussing status codes in general, the correct reference is Section 15 of HTTP. Use ‘status code’, not ‘Status Code’.

### Example Messages

If your specification has examples of HTTP messages (and it probably should), they should give enough context for readers to understand. Generally, this means showing a substantial portion of the message; e.g., not just a header field in isolation, but an entire request or response message (with a truncated body if applicable). Where appropriate, an entire exchange (request and response) can be illustrated using two subsequent example sections.

Examples should be in HTTP/1.1 format unless they are specific to another version of the protocol. HTTP/1.1 examples should be labeled with the `http-message` type so that the validator can check them.

For example (in Markdown):

~~~ http-message
HTTP/1.1 200 OK
Content-Type: text/plain
Example-Header: foo

[ content ]
~~~

The [rfc-http-validate](https://github.com/mnot/rfc-http-validate) tool (Python package) can be used to validate draft examples marked in this fashion.

Examples with long lines (over 72 characters) should be wrapped using the line folding convention where possible. For example:

~~~ http-message
HTTP/1.1 200 OK
Content-Type: text/plain
Example-Header: this content is very long so we
   fold it to the next line and indent

[ content ]
~~~

If the too-long content cannot include whitespace, use RFC 8792 encoding:

~~~ http-message
# NOTE: '\' line wrapping per RFC 8792

HTTP/1.1 200 OK
Content-Type: text/plain
Example-Header: abcedfghijlkmnopqrstuvwxyzabcedfghijlkmnopqrstuvwxyzabc\
     edfghijlkmnopqrstuvwxyz

[ content ]
~~~

Note that the notice header has to occur on each section that uses this encoding.

### References

Generally, named references are preferred for “core” specifications like HTTP and TLS. In addition to giving readers a cue about the purpose of the reference, this is a small hint that the RFC number is not the identifier they should be remembering. For example:

> This document defines a HTTP {{HTTP}} header field that uses the conventions
> in {{STRUCTURED-FIELDS}} to convey information about the TLS ({{TLS}}) session.

The following reference names are preferred (but not required):

* HTTP - HTTP Semantics (RFC 9110)
* HTTP-CACHING - HTTP Caching (RFC 9111)
* HTTP/1.1 - HTTP/1.1 (RFC 9112)
* HTTP/2 - HTTP/2 (RFC 9113)
* HTTP/3 - HTTP/3 (RFC 9114)
* STRUCTURED-FIELDS - Structured Field Values for HTTP (RFC 9651)
* COOKIES - Cookies: HTTP State Management Mechanism (RFC 6265)
* TLS - The Transport Layer Security (TLS) Protocol Version 1.3 (RFC 9846)

Specifications MUST reference the most recent RFC (see above). Use RFC 9110 as the primary reference for HTTP; referencing the rest of the suite is only necessary when a specific feature is called out.

Note that to include / in an anchor name in the kramdown-rfc flavor of markdown, the reference needs to be declared in the YAML header like this:

~~~yaml
normative:
  RFC9112:
    display: HTTP/1.1
~~~
