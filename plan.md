# Project: Terminal-Based Bandcamp Music Player

## 1. Project Overview

Build a polished, fully terminal-based music player for listening to music in the user's own Bandcamp collection.

The application should provide a pleasant, modern terminal user interface for browsing an existing Bandcamp music library, searching for artists, albums and tracks, and playing music without requiring a graphical desktop application or a web browser.

The initial implementation should focus on the essential functionality of a good music player rather than attempting to reproduce every feature of a full desktop music application.

The working project name is **Campfire**. Treat the name as configurable during development; the final application name can be decided later.

Reference project:
https://github.com/eremef/bandcamp-player

Study this repository to understand its relevant functionality, particularly how it accesses a user's collection and handles playback. Use it as a reference for behavior and architecture, not as a reason to reproduce its entire implementation.

The desired result is a small, maintainable, reliable application that feels like a purpose-built terminal music player.

## 2. Core Principles

Follow these principles throughout development:

1. Prefer Python and its standard library.
2. Avoid unnecessary dependencies.
3. Introduce an external dependency only when it provides a meaningful advantage or is necessary for reliable functionality.
4. Verify that every proposed third-party library exists, is maintained sufficiently for the intended use, and supports the target environment.
5. Inspect existing solutions and available APIs before implementing custom alternatives.
6. Prefer official interfaces over scraping websites or relying on undocumented behavior.
7. Keep the architecture modular without introducing unnecessary abstraction layers.
8. Implement working functionality rather than placeholders, pseudocode or simulated behavior.
9. Handle network failures, authentication errors, unavailable tracks and playback failures gracefully.
10. Prioritize a polished terminal experience, clear navigation and reliable playback over an excessive number of features.

Do not add features simply because they are common in other music players. Implement the features specified in this document first.

## 3. Initial Research and Technical Feasibility

Before writing application code, inspect the reference repository and research the available Bandcamp integration options.

### 3.1 Investigate Bandcamp's Subsonic API

Bandcamp announced a Subsonic API implementation in July 2026.

Official announcement:
https://blog.bandcamp.com/2026/07/16/discover-improvements-and-subsonic-implementation/

The announced server endpoint is:

https://bandcamp.com/api/subsonic

Investigate whether the current implementation supports the operations needed by this application.

At minimum, investigate:

- Authentication and credential setup.
- Retrieving the user's collection.
- Browsing albums and tracks.
- Obtaining artist information.
- Searching the collection.
- Streaming tracks.
- Retrieving track durations and other metadata.
- Navigating albums and their track listings.
- Determining whether track URLs or identifiers remain valid over time.
- Error responses and authentication failures.
- Pagination and large collections.
- Whether any additional requests are necessary to retrieve useful artist or album metadata.

Consult current official documentation and the actual available API behavior.

Do not assume that every standard Subsonic API operation is supported by Bandcamp merely because Bandcamp implements the protocol.

Determine the simplest reliable way to use the interface from Python.

Prefer an implementation using the Python standard library's HTTP facilities if they are sufficient. If an external HTTP library would materially improve reliability or maintainability, explain why before adopting it.

### 3.2 Investigate Audio Playback

Determine how to play audio reliably while keeping the application's interface entirely inside the terminal.

Evaluate these approaches:

1. Using an installed external audio player, such as mpv, controlled by the Python application.
2. Using another established system audio backend if it offers a clear advantage.
3. Implementing playback directly in Python only if a suitably small and reliable solution exists.

Prefer an existing audio player over implementing audio decoding, buffering and output from scratch.

The application must not require a graphical audio-player window.

If mpv is selected, investigate its command-line options and IPC interface. Use its supported interfaces rather than attempting to control playback through fragile terminal automation.

The playback backend should ideally support:

- Starting and stopping playback.
- Pausing and resuming.
- Seeking forwards and backwards.
- Reading the current playback position and duration.
- Changing volume.
- Detecting when a track ends.
- Starting the next track.
- Reporting playback errors.

If the selected backend cannot provide one of these capabilities reliably, document the limitation and implement the best practical alternative.

Do not bundle or download an external executable automatically without the user's explicit consent.

### 3.3 Dependency Decision

Prepare a short dependency assessment before implementation.

For each proposed external dependency, identify:

- Its purpose.
- Why the standard library is insufficient.
- Its official project or documentation.
- Its installation requirements.
- Whether it is optional or mandatory.

Prefer a design in which the core application can operate with a minimal Python environment and an explicitly documented playback backend.

Do not introduce a large terminal UI framework, audio framework or general-purpose application framework unless there is a compelling technical reason.

## 4. Target Environment

Initially target:

- Linux.
- macOS.
- Modern Python 3.11 or later.

Use portable Python code wherever practical.

The interface must work in ordinary modern terminal emulators.

Do not assume that every terminal supports advanced graphics, sixel, Kitty graphics or similar extensions.

Support graceful degradation when optional terminal capabilities are unavailable.

The application should work in a normal terminal window without requiring a graphical desktop environment beyond whatever is necessary for the system's audio output.

## 5. Terminal User Interface

Create a modern, polished terminal interface.

The design should feel like a compact music player rather than a traditional administrative command-line application.

Use restrained colors, good spacing, consistent alignment and clear visual hierarchy.

Avoid excessive ASCII art, decorative borders, unnecessary animations and gimmicky visual effects.

The default theme must be black and orange.

Use orange for important highlights, the currently playing track, active selections, progress indicators and other meaningful accents.

Use neutral foreground colors for ordinary text. Ensure readable contrast and avoid excessive use of bright colors.

### 5.1 Main Layout

Design a responsive terminal layout containing the following logical areas where screen space permits:

- Application header.
- Current view or navigation context.
- Main content area.
- Now Playing section.
- Playback progress indicator.
- Playback controls and keyboard shortcuts.
- Optional status or notification line.

A possible layout is:

    CAMPFIRE                                      [SEARCH]

    LIBRARY
      Artists
      Albums
      Tracks

    COLLECTION
      Artist or album listing
      Search results
      Track listing

    NOW PLAYING
      Artist Name — Track Title
      Album Name
      [==========-------------] 03:12 / 05:48

    SPACE Play/Pause   ←/→ Seek   N Next   P Previous
    A Artists          L Albums   / Search  Q Quit

This is only an illustration. Design the actual interface according to terminal dimensions and usability.

Do not force a rigid multi-column layout on small terminals.

Adapt the interface to the available terminal width and height. On smaller screens, simplify the layout or display fewer panels rather than producing broken alignment.

### 5.2 Navigation

Provide clear navigation between:

- Main library.
- Artists.
- Albums.
- Tracks.
- Search results.
- Current playback queue.

The user should be able to navigate using the keyboard without a mouse.

Use consistent shortcuts and display the relevant shortcuts in the interface.

Where practical, support arrow keys, Enter, Escape and intuitive single-key commands.

Avoid assigning too many shortcuts that conflict with text input.

When a search field is active, ordinary text input must work correctly and must not accidentally trigger playback shortcuts.

### 5.3 Keyboard Shortcuts

Implement a coherent initial shortcut scheme.

Suggested defaults:

- Space: Play or pause.
- Left arrow: Seek backwards.
- Right arrow: Seek forwards.
- N: Next track.
- P: Previous track.
- A: Browse artists.
- L: Browse albums.
- T: Browse tracks.
- /: Open search.
- Q: Quit.

You may adjust these shortcuts if terminal compatibility or usability requires it.

Provide a visible shortcut reference.

Do not assume that every terminal transmits every key identically. Test keyboard handling and document any unavoidable limitations.

## 6. Music Library

The application must display the user's own Bandcamp collection.

### 6.1 Artists

Provide an artist browser.

The user must be able to:

- View artists represented in their collection.
- Navigate through the artist list.
- Search or filter artists.
- Select an artist.
- View albums and tracks associated with that artist.
- Play an artist's available music.

If an artist has several albums, allow the user to play a selected album or start playback from the artist's available tracks.

Avoid duplicating artist entries unnecessarily when multiple albums refer to the same artist.

### 6.2 Albums

Provide an album browser.

Display useful metadata where available:

- Album title.
- Artist.
- Release date, if available.
- Number of tracks.
- Other useful metadata returned by the API.

Allow the user to:

- Open an album.
- Browse its track listing.
- Play the entire album.
- Play a selected track.
- Start playback from a selected track and continue through the remaining album tracks.

If artwork is available, do not require image-rendering support. Text-based album information is sufficient for the initial release.

### 6.3 Tracks

Provide a track browser containing tracks available in the user's collection.

Display relevant information such as:

- Track title.
- Artist.
- Album.
- Duration, when available.

Support selection, playback and searching.

### 6.4 Local Caching

Investigate whether caching the retrieved library locally would improve startup time and browsing performance.

If useful, implement a small local cache using Python's built-in SQLite support.

The cache must not become an unnecessary second source of truth.

Clearly separate cached metadata from live authentication and streaming operations.

Provide a way to refresh the library.

Handle expired or missing cache data gracefully.

Do not download and permanently store audio files as part of the initial implementation.

The initial goal is streaming music from the user's collection, not building an offline music downloader.

## 7. Search

Implement a unified library search.

The user should be able to search for:

- Artists.
- Albums.
- Tracks.

Search results should clearly identify the result type and provide enough metadata to distinguish similarly named items.

Allow the user to select a result and navigate to its artist, album or track.

Prefer fast local searching over repeated network requests when the necessary metadata is already cached.

If the API provides useful server-side search functionality, evaluate whether it should complement local searching.

Search must remain usable with a large collection.

Provide a clear way to cancel a search and return to the previous view.

## 8. Audio Playback

Implement a proper playback controller.

Required functionality:

- Play.
- Pause.
- Resume.
- Stop.
- Next track.
- Previous track.
- Seek forwards and backwards.
- Display the current playback position.
- Display track duration when available.
- Display the current artist, track and album.
- Automatically advance to the next track when the current track ends.
- Report playback and streaming errors clearly.

Use a playback queue internally, even if the initial interface exposes only basic queue controls.

The queue should support:

- Playing an album in track order.
- Playing an artist's available tracks.
- Starting playback from a selected track.
- Moving to the next track.
- Returning to the previous track.
- Displaying upcoming tracks.
- Handling unavailable tracks without crashing.

For the previous-track command, implement sensible behavior: restart the current track when it has progressed sufficiently, otherwise return to the previous track in the queue. Make the threshold configurable if this is straightforward.

Do not allow the UI to freeze while a network request or playback operation is running.

Keep playback state and UI state synchronized.

When the player is stopped or paused, the displayed state must accurately reflect what the playback backend reports.

### 8.1 Playback Reliability

Handle these cases explicitly:

- Missing or invalid credentials.
- Network disconnection.
- Expired authentication.
- Track unavailable for streaming.
- API errors.
- Audio backend not installed.
- Audio backend unexpectedly exiting.
- Invalid playback URLs.
- Seeking unsupported by a particular stream.
- Terminal closure or application interruption.

Do not silently pretend that playback succeeded.

Show useful error messages without exposing passwords, tokens or other authentication secrets.

## 9. Audio Visualizations

Implement a small number of simple audio visualizations if they can be added without introducing substantial complexity.

Possible options:

1. A clean playback progress bar.
2. An animated equalizer-style meter.
3. A simple waveform or spectrum-style visualization.

The progress bar is mandatory.

The other visualizations are optional and should only be implemented if a technically sound approach is available.

Do not generate fake audio-reactive data and present it as a real spectrum analysis.

Investigate whether the selected playback backend exposes audio data or useful visualization information.

If actual audio analysis requires additional dependencies or a complicated audio pipeline, prefer a simpler implementation.

Any visualization must be lightweight, optional and compatible with the default terminal layout.

The application must remain fully usable when visualizations are disabled.

## 10. Theme System

Implement a file-based theme system.

The theme format is your technical decision. Choose JSON, TOML or YAML according to the project's dependency requirements and maintainability.

Prefer TOML if it meets the requirements without additional dependencies, because modern Python versions include a TOML parser in the standard library.

Themes should be stored in a dedicated directory, for example:

    themes/
      theme_default.toml
      theme_dracula.toml

These are illustrative filenames.

Use a configuration file to select the active theme.

For example:

    theme = "theme_default.toml"

The exact configuration filename and format are up to you.

The configuration should live in an appropriate user configuration directory rather than requiring users to edit application source code.

### 10.1 Theme Properties

A theme should be able to define the main visual properties, including:

- Background color.
- Primary text color.
- Secondary text color.
- Accent color.
- Highlighted selection colors.
- Current track colors.
- Progress bar colors.
- Border colors, if borders are used.
- Error and warning colors.
- Muted or inactive text colors.

Keep the theme schema simple.

Do not allow themes to execute code or load arbitrary plugins.

Validate theme files and provide useful error messages for invalid configurations.

If a selected theme is missing or invalid, fall back to the default theme.

### 10.2 Default Theme

Create a polished black-and-orange default theme.

The design should be modern, understated and readable.

Use orange accents selectively rather than making every interface element orange.

The current track, active navigation item and progress indicator should be easy to identify.

Avoid excessive decoration and unnecessary terminal effects.

Include at least one additional example theme if it can be implemented without introducing extra complexity.

The Dracula-inspired theme is a reasonable example.

## 11. Authentication and Configuration

Determine the correct authentication mechanism by investigating Bandcamp's current Subsonic implementation.

Do not invent an authentication flow or assume that generic Subsonic instructions work unchanged with Bandcamp.

Use the officially supported authentication method.

Credentials must not be stored in source code or committed to version control.

Prefer an appropriate local configuration directory and secure credential storage where available.

If the API requires credentials that cannot be stored safely using the standard library alone, document the trade-off and choose a sensible solution.

Never print passwords, access tokens or other secrets in logs.

Provide clear setup instructions for first-time authentication.

If the user must create credentials through Bandcamp's settings, explain the exact process using verified instructions.

The application must fail gracefully when credentials are missing or invalid.

## 12. Configuration and Data Storage

Keep user configuration separate from application code.

Use an appropriate platform-specific configuration directory.

A configuration file may contain:

- Selected theme.
- Playback settings.
- Seek interval.
- Cache settings.
- Other small user preferences.

Do not add configuration options without a practical purpose.

Use a sensible default for every optional setting.

Store persistent library metadata in SQLite only if local caching is implemented.

Avoid introducing a database migration framework unless the project genuinely needs one.

Ensure that cache corruption or an incompatible cache version does not prevent the application from starting.

## 13. Suggested Architecture

Use a small, modular architecture.

A possible structure is:

    campfire/
      __main__.py
      app.py

      config/
        settings.py
        theme.py

      bandcamp/
        client.py
        library.py
        models.py

      playback/
        controller.py
        backend.py
        queue.py

      ui/
        terminal.py
        layout.py
        navigation.py
        views.py
        input.py
        visualizations.py

      storage/
        cache.py

      themes/
        theme_default.toml
        theme_dracula.toml

    tests/
    docs/
    pyproject.toml
    README.md

This structure is illustrative, not mandatory.

Adjust it according to the actual implementation.

Keep the API client, playback controller, persistent storage and terminal UI separated.

The UI should not directly implement HTTP requests or audio backend commands.

The playback controller should provide a clear interface to the rest of the application.

Use Python type annotations where they improve clarity.

Avoid unnecessary classes, interfaces, abstractions and design patterns.

The project should remain easy to understand and modify.

## 14. Terminal Compatibility and Shutdown

Handle terminal lifecycle correctly.

When the application starts, initialize the terminal interface cleanly.

When it exits, restore terminal settings, cursor visibility and terminal modes.

The terminal must be restored even if an exception occurs or the user interrupts the application.

Handle Ctrl+C and normal quit commands gracefully.

Avoid leaving the terminal in an unusable state after a crash.

Use the standard library's terminal capabilities when they are sufficient.

If a third-party terminal UI library is necessary, explain why and verify its compatibility before adding it.

## 15. Performance

The application should start quickly and remain responsive.

Avoid unnecessarily refreshing the library on every screen transition.

Cache metadata where appropriate.

Do not block the terminal rendering loop while waiting for network responses.

Do not repeatedly redraw the entire terminal when only a small area needs updating, unless full redraws are required by the chosen implementation.

Avoid unnecessary CPU usage when playback is paused or the application is idle.

Visualizations must have a sensible refresh rate and should not dominate CPU usage.

Ensure that playback continues while the user browses the library or searches for music.

## 16. Testing

Implement meaningful tests rather than relying exclusively on manual testing.

At minimum, test:

- Configuration loading.
- Default configuration generation.
- Theme parsing and validation.
- Invalid theme fallback.
- Library model parsing.
- Search behavior.
- Queue operations.
- Next and previous track behavior.
- Playback state transitions.
- Error handling.
- Cache operations, if implemented.

Mock network interactions in unit tests.

Do not require working Bandcamp credentials for ordinary automated tests.

Do not make tests dependent on a live music stream.

Where practical, provide a small integration test or documented manual verification procedure for the actual Bandcamp API and playback backend.

Clearly distinguish automated tests from manual tests.

Do not claim that an integration has been verified unless it has actually been tested.

## 17. Documentation

Create a useful README that covers:

- Project overview.
- Supported platforms.
- Python version requirements.
- External dependencies and their purposes.
- Installation.
- Audio backend installation.
- Bandcamp authentication setup.
- Configuration locations.
- Theme creation and selection.
- Available keyboard shortcuts.
- Running the application.
- Running automated tests.
- Troubleshooting common problems.
- Known limitations.

Provide exact commands for installation and execution.

Do not document unimplemented features as if they already exist.

Keep the instructions consistent with the actual code.

## 18. Implementation Phases

Work incrementally.

### Phase 1: Research and Design

Inspect the reference repository.

Investigate the current Bandcamp Subsonic API implementation and verify the authentication and playback workflow.

Determine the minimal dependency set.

Choose the playback backend and terminal UI approach.

Write a concise technical plan describing the decisions and their rationale.

Identify any blockers before committing to an architecture.

### Phase 2: Minimal Working Playback

Implement the smallest working end-to-end application.

It must be able to:

1. Load configuration.
2. Authenticate with Bandcamp.
3. Retrieve the user's collection.
4. Select an available track.
5. Play the track.
6. Pause and resume playback.
7. Display basic Now Playing information.
8. Exit cleanly.

Verify that this works before building the complete interface.

### Phase 3: Library and Search

Implement artist, album and track browsing.

Add search and the playback queue.

Support playing individual tracks, albums and an artist's available music.

### Phase 4: Terminal UI

Implement the complete keyboard-driven interface.

Add the playback progress bar, navigation, responsive layout and clear shortcut hints.

Polish the visual design.

### Phase 5: Themes and Persistence

Implement the file-based theme system.

Add the black-and-orange default theme and an example alternative theme.

Implement local metadata caching if the research phase demonstrated a clear benefit.

### Phase 6: Visualizations and Polish

Implement optional visualizations only if they can be done reliably without compromising the core application.

Improve error handling, terminal compatibility and performance.

Remove unnecessary complexity.

### Phase 7: Validation and Documentation

Run the automated tests.

Perform an end-to-end playback test where valid credentials and network access are available.

Verify the installation and startup instructions.

Document all remaining limitations.

## 19. Scope Boundaries

The initial release does not require:

- A graphical user interface.
- A web interface.
- A mobile application.
- Chromecast support.
- Remote control.
- Offline music downloading.
- Audio file conversion.
- Music tagging.
- Last.fm integration.
- Discord integration.
- Cloud synchronization.
- A complex playlist editor.
- A plugin ecosystem.
- Advanced audio effects.
- A sophisticated spectrum analyzer.

Do not implement these features unless a core requirement depends on them.

The objective is a good terminal music player, not a feature-for-feature clone of the reference project.

## 20. Definition of Done

The project is complete when all of the following are true:

- The application launches from the terminal.
- The user can authenticate using the verified Bandcamp integration.
- The user's own collection can be browsed.
- Artists, albums and tracks can be searched.
- An artist's available music can be played.
- An album can be played in track order.
- Individual tracks can be selected and played.
- Play, pause, resume, next, previous and seeking work where supported by the backend.
- Now Playing displays accurate playback information.
- The playback queue behaves consistently.
- The terminal interface remains responsive during network operations.
- The black-and-orange default theme works.
- Themes can be changed through configuration files.
- Invalid configuration and theme files are handled gracefully.
- The terminal is restored properly on exit.
- The application handles common network, authentication and playback errors.
- Automated tests cover the important application logic.
- Installation and usage instructions are complete.
- External dependencies are documented and justified.
- No core feature is represented by a placeholder or simulated implementation.

If a requirement cannot be implemented reliably because of a limitation in Bandcamp's API, the operating system or the selected playback backend, document the limitation clearly instead of faking the functionality.

## 21. Working Instructions for the Coding Agent

Work autonomously within the requirements above.

Inspect the repository and the available environment before making implementation decisions.

Verify current API behavior and library documentation instead of relying on assumptions.

Make reasonable implementation decisions without asking for approval for every minor detail.

Prefer small, working increments over a large untested implementation.

After each meaningful phase, run the relevant tests and fix regressions before proceeding.

Do not overwrite unrelated files or introduce unnecessary changes to the development environment.

Do not add dependencies merely because they are popular or convenient.

Do not create fake API responses, placeholder playback implementations or misleading visualizations to make the interface appear functional.

If an external dependency is unavoidable, verify its official source, compatibility and installation requirements before adopting it.

Keep the implementation maintainable and the user experience polished.

When finished, provide a concise implementation summary containing:

1. What was implemented.
2. Important architectural decisions.
3. External dependencies and why they were necessary.
4. Installation and startup instructions.
5. Test results.
6. Any remaining limitations.

Begin with research and technical feasibility. Then proceed with implementation.