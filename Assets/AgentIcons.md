# Agent icons

The SVGs in `Sources/AgentIcons.xcassets` are official brand artwork, downloaded November 9, 2026:

- Claude: https://claude.com/icon.svg — original sunburst geometry and color.
- Codex: OpenAI Blossom SVG from https://developers.openai.com/codex (header). OpenAI also uses the Blossom as the Codex app icon on https://chatgpt.com/codex/. The SVG's `1em` dimensions are normalized to 16 points and `currentColor` to black; geometry is unchanged. It renders as a template for light/dark appearance.

Claude and its mark belong to Anthropic. Codex and the OpenAI Blossom belong to OpenAI. These assets identify the corresponding agents, and are not covered by this repository's MIT license. OpenAI's brand guidelines: https://openai.com/brand/.

Both SVGs use a 16-point intrinsic width and height while retaining their original view boxes and path geometry. Native macOS menus use the asset’s intrinsic size rather than the SwiftUI image frame; keep these dimensions at 16 to avoid oversized menu items.
