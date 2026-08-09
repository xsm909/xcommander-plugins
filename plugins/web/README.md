# Web resources

A page as a folder of the things it is made of. Point a panel at a site and you
get its images, scripts, stylesheets and fonts as files, and the pages it links
to as folders you can walk into.

Because it is an ordinary file system as far as the application is concerned,
everything else already works: **F3** views a resource, **F5** copies it to the
other panel, the search finds things, and the disk map will measure a whole site
if you let it.

## Getting there

- **Go to** (Ctrl+P or the path bar) and type `https://example.com/`.
- Or save one under **Drives and connections** → *Web site*.

## Read and copy, never write

`write`, `mkdir`, `delete` and `rename` are not implemented, so the application
refuses them the way it refuses anything a backend cannot do. That is deliberate
and not an oversight: a tool for looking at somebody else's site should not have
a working delete key.

## What counts as what

A suffix decides when there is one — a link to a `.pdf` is a file however it was
written. Without a suffix, what it was referenced *as* decides: `<a>` and
`<iframe>` are somewhere to go, everything else is something the page is built
from. References are collected from `src`, `href`, `data`, `poster`, `srcset`
and from `url(...)` inside a `<style>` block, which is where a page's images
usually hide.

A resource from another host keeps that host in front of its name, because
`logo.png` from three CDNs is three files and one name.

## Settings

| | |
| --- | --- |
| Show the pages it links to | Links become folders. Off leaves only the parts. |
| Include resources from other hosts | A page usually pulls fonts and scripts from elsewhere. |
| Follow links to other hosts | Off keeps a walk inside the site you opened; off-site links are still listed, as files. |
| Ask each resource how big it is | One HEAD per resource, eight at a time. Off leaves sizes blank until something is opened or copied. |
| Respect robots.txt | What the site asks automated readers not to fetch. Turn it off only on a site you run. |
| Give a request at most | Seconds. |
| Identify as | The User-Agent. Some sites answer differently without one they recognise. |

## Worth knowing

- **Ranges.** Reading asks for a byte range and for no compression, so offsets
  mean what they say. A server that ignores the range and sends the whole file
  is handled by keeping that body until the read is finished — one request
  rather than one per chunk.
- **Cycles.** Pages link to each other, so walking a site by hand can go round
  in circles; nothing here keeps a visited set. That is fine for a panel, where
  every step is yours, and worth remembering before pointing something automatic
  at it.
- **A page is fetched again each time it is listed.** There is no cache: what
  you see is what the server says now.
