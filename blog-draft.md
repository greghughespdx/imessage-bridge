# iMessage for AI Agents, Without the Nonsense

My homelab runs two Claude Code agents. Albert lives on my Mac Studio and handles orchestration and architecture. Hal lives on my MacBook Pro and handles implementation work. They collaborate via a message bus, work through problems together overnight, and generally operate as a two-person (two-agent?) team.

The obvious next step was getting them onto iMessage. I want my AI agents communicating over the same channels I use, not over custom internal protocols I have to maintain separately. iMessage is already on every device I own, including CarPlay. If a human can be in an iMessage thread, an AI should be able to be in one too.

The problem is that most iMessage integrations are a mess.

## The Existing Options Are Rough

BlueBubbles is the most popular open-source iMessage bridge. It works, people use it, and it has a real community around it. But it hooks into Apple's private frameworks, requires disabling System Integrity Protection, and has a known habit of crashing the Messages app. Beeper and pypush went the same direction: private APIs, unstable, and Apple has both the motive and the ability to break them at any point.

I don't want to maintain a system that Apple can nuke in an OS update. And I definitely don't want to run something that requires SIP modifications on my daily driver.

Here is the thing though: Apple already gives us two perfectly good, public, stable interfaces for iMessage. They have for over a decade.

The first is `~/Library/Messages/chat.db`. iMessage stores your entire message history in a plain SQLite database. No private APIs. No reverse engineering. Just a database file that your backup tools, forensics software, and any number of automation scripts have been reading for years. Apple knows this happens. They have no reason to lock it down.

The second is AppleScript. Messages has had an AppleScript dictionary since Mountain Lion. `tell application "Messages" to send "hello" to buddy "+15034102254"` just works. It has worked for twelve years. This is not a hack. It is a documented automation interface.

Reading from chat.db plus sending via AppleScript: that is the entire iMessage integration. No private frameworks. No SIP modifications. No application stack to maintain.

## What I Built in One Session

The core is a Python bridge server that weighs in at about 200 lines and has zero external dependencies. Pure stdlib. It watches chat.db for new messages (with a short polling interval), serves them over a simple HTTP API, and accepts send requests that it forwards to Messages via AppleScript.

That is the whole bridge. There is no database of its own. There is no state to manage beyond a cursor into chat.db. It runs as a background service via launchd or Homebrew services, consumes negligible resources, and has nothing to break.

On top of that bridge, I built a Claude Code channel server plugin that connects iMessage to Claude Code sessions as native channel events. The same interface Claude Code uses for Telegram and Slack channels. The agents see iMessage messages as channel events, respond to them, and the responses go back out through the bridge as real iMessages.

The channel server supports two modes:

**Local mode** is for when the bridge and the Claude Code session are on the same machine. It reads chat.db directly without any HTTP layer. Zero middleware. The channel server just queries the database itself.

**Remote mode** is for when they are on different machines. The channel server polls the bridge over HTTP. This is how Albert (on Mac Studio) connects to the bridge running on my MacBook Pro, for example.

Bonjour/mDNS auto-discovery handles finding bridges on the local network automatically. The bridge advertises itself as an `_imessage-bridge._tcp` service. The channel server looks for it. No IP configuration needed.

## What Works

Group chats work out of the box. SMS forwarding (green bubbles from non-Apple devices) works via Text Message Forwarding, same as it does on any Mac with that feature enabled. Multiple bridges on one network work with named Bonjour services so they do not collide.

CarPlay works automatically. Since this goes through the actual Messages app, anything that works with iMessage works here. The agents' messages show up as real iMessages from the bridge machine's account.

One bridge can serve any number of clients. Albert and Hal can both be connected to the same bridge and participate in the same threads.

The test I ran was three-way: Hal, Albert, and me in an iMessage group thread. Two AI agents and a human, coordinating via iMessage like it is a normal thing. It is, as of this session.

## The Bug I Found Along the Way

During testing, I noticed the Claude Code channel harness was spawning duplicate plugin processes. The channel plugin was getting launched twice, which caused doubled message delivery. I instrumented the startup sequence, confirmed the behavior, and filed it as GitHub issue #36800 with the diagnostic logs attached.

That kind of thing is part of building on top of a platform that is still evolving. The channel system is relatively new in Claude Code. Finding the edge cases and reporting them is useful for everyone building channel integrations.

## Why This Approach Is Different

Every other iMessage integration I am aware of adds middleware and/or uses private APIs. BlueBubbles is a full application. Beeper was building an entire messaging platform. pypush was doing protocol-level reverse engineering.

This is the first iMessage integration I know of that can work with genuinely zero middleware in local mode. When the bridge and the session are on the same machine, the channel server reads chat.db directly, sends via AppleScript, and the only running process is the Claude Code session itself.

For remote mode, the "middleware" is 200 lines of stdlib Python. Compare that to running a full application stack with its own database, frontend, and service dependencies.

Apple has no reason to lock down chat.db reads. Backup software, parental control apps, forensics tools, and a long list of automation utilities have been reading that file for years. AppleScript support for Messages is explicitly documented and maintained. These are not loopholes. They are features.

## The Bigger Picture

I want my AI agents to be participants in the same communication channels I use, not isolated behind internal APIs I have to build and maintain. iMessage is where I actually talk to people. Telegram is where some of my automation and monitoring already sends alerts. The agents should be reachable there.

The Claude Code channel system makes this composable. One channel integration per service (iMessage, Telegram, Slack, whatever), and any agent can be connected to any of them. The agent does not need to know it is talking over iMessage specifically. It just sees channel events.

This session went from "this should be possible" to working three-way group chat. The architecture is simple enough that the whole thing fits in your head. That is how it should be.

## Get Started

Install via Homebrew:

```bash
brew tap greghughespdx/tools
brew install imessage-bridge
brew services start imessage-bridge
```

The bridge will start on port 5001 and advertise itself via Bonjour. If you are running Claude Code on the same machine, the local mode channel server will find chat.db automatically.

The GitHub repo has the channel server plugin, the Homebrew formula, and documentation for remote mode setup with multiple agents.

Source: [github.com/greghughespdx/imessage-bridge](https://github.com/greghughespdx/imessage-bridge)

MIT license. Contributions welcome.
