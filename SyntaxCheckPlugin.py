import sublime, sublime_plugin
import subprocess
import os
import html
import json
import webbrowser
from pprint import pprint

"""On-save syntax checking.

This contains the code for displaying message phantoms for errors/warnings
whenever you save a Rust file.
"""

# Notes:
# - -Zno-trans produces a warning about being unstable (see
#   https://github.com/rust-lang/rust/issues/31847). I am uncertain about the
#   long-term prospects of how this will be resolved.  There are a few things
#   to consider:
#       - Cargo recently added "cargo check"
#         (https://github.com/rust-lang/cargo/pull/3296), which more or less
#         does the same thing.  See also the original "cargo check" addon
#         (https://github.com/rsolomo/cargo-check/).
#       - RLS was recently released
#         (https://github.com/rust-lang-nursery/rls).  It's unclear to me if
#         this will perform full-linting that could replace this or not.
#
# - -Zno-trans prevents some warnings and errors from being generated. For
#   example, see const-err.rs.  "cargo check" will solve this, but it is
#   nightly only right now. Other issues:
#       - Errors generated by compiling an extern crate do not not output as
#         json.

# TODO:
# - clippy support (doesn't output json afaik)
# - Some way to navigate to messages?  Similar to Build next/previous.

class rustPluginSyntaxCheckEvent(sublime_plugin.EventListener):

    # Beware: This gets called multiple times if the same buffer is opened in
    # multiple views (with the same view passed in each time).  See:
    # https://github.com/SublimeTextIssues/Core/issues/289
    def on_post_save_async(self, view):
        # Are we in rust scope and is it switched on?
        # We use phantoms which were added in 3118
        if int(sublime.version()) < 3118:
            return

        settings = view.settings()
        enabled = settings.get('rust_syntax_checking')
        if enabled and "source.rust" in view.scope_name(0):
            file_name = os.path.abspath(view.file_name())
            file_dir = os.path.dirname(file_name)
            os.chdir(file_dir)

            view.set_status('rust-check', 'Rust syntax check running...')
            # This flag is used to terminate early. In situations where we
            # can't auto-detect the appropriate Cargo target, we compile
            # multiple targets.  If we receive any messages for the current
            # view, we might as well stop.  Otherwise, you risk displaying
            # duplicate messages for shared modules.
            self.this_view_found = False
            try:
                self.hide_phantoms(view.window())

                # Keep track of regions used for highlighting, since Sublime
                # requires it to be added in one shot.
                # Key is view.id, value is
                #     {'view': view, 'regions': [(scope, region)]}
                regions_by_view = {}

                for target_src, infos in self.get_rustc_messages(settings, file_name):
                    # print('-------------')
                    for info in infos:
                        # pprint(info)
                        self.add_error_phantoms(view, info, settings, regions_by_view, target_src, {})
                    if self.this_view_found:
                        break

                self.draw_region_highlights(regions_by_view)
            finally:
                view.erase_status('rust-check')

        # If the user has switched OFF the plugin, remove any phantom lines
        elif not enabled:
            self.hide_phantoms(view.window())
        # print('done')

    def run_cargo(self, args):
        """Args should be an array of arguments for cargo.
        Returns list of dictionaries of the parsed JSON output.
        """
        # When sublime is launched from the dock in OSX, it does not have the user's environment set. So the $PATH env is reset.
        # This means ~./cargo/bin won't be added (causing rustup to fail), we can manually add it back in here. [This is a hack, hopefully Sublime fixes this natively]
        # fixes https://github.com/rust-lang/sublime-rust/issues/126
        env = os.environ.copy() # copy so we don't modify the current processs' environment
        normalised_cargo_path = os.path.normpath("~/.cargo/bin") + (";" if os.name == "nt" else ":")
        env["PATH"] = normalised_cargo_path + env["PATH"]

        cmd = ' '.join(['cargo']+args)
        print('Running %r' % cmd)
        # shell=True is needed to stop the window popping up, although it looks like this is needed:
        # http://stackoverflow.com/questions/3390762/how-do-i-eliminate-windows-consoles-from-spawned-processes-in-python-2-7
        cproc = subprocess.Popen(cmd,
            shell=True, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, env=env)
        output = cproc.communicate()
        output = output[0].decode('utf-8')  # ignore errors?
        result = []
        for line in output.split('\n'):
            if line == '' or line[0] != '{':
                continue
            result.append(json.loads(line))
        # print(output)
        if not result and cproc.returncode:
            print('Failed to run: %s' % cmd)
            print(output)
        return result

    def get_rustc_messages(self, settings, file_name):
        """Top-level entry point for generating messages for the given
        filename.  A generator that yields (target_src_filename, infos)
        tuples, where:
        * target_src_filename: The name of the top-level source file of a
          Cargo target.
        * infos: A list of JSON dictionaries produced by Rust for that target.
        """
        targets = self.determine_targets(settings, file_name)
        for (target_src, target_args) in targets:
            args = ['rustc', target_args, '--',
                    '-Zno-trans', '-Zunstable-options', '--error-format=json']
            if (settings.get('rust_syntax_checking_include_tests', True) and
                '--test' not in target_args
               ):
                args.append('--test')
            yield (target_src, self.run_cargo(args))

    def determine_targets(self, settings, file_name):
        """Detect the target/filters needed to pass to Cargo to compile
        file_name.
        Returns list of (target_src_path, target_command_line_args) tuples.
        """
        # Try checking for target match in settings.
        result = self._targets_manual_config(settings, file_name)
        if result: return result

        # Try a heuristic to detect the filename.
        output = self.run_cargo(['metadata', '--no-deps'])
        if not output:
            return []
        # Each "workspace" shows up as a separate package.
        for package in output[0]['packages']:
            root_path = os.path.dirname(package['manifest_path'])
            targets = package['targets']
            # targets is list of dictionaries:
            # {'kind': ['lib'],
            #  'name': 'target-name',
            #  'src_path': 'path/to/lib.rs'}
            # src_path may be absolute or relative, fix it.
            for target in targets:
                if not os.path.isabs(target['src_path']):
                    target['src_path'] = os.path.join(root_path, target['src_path'])
                target['src_path'] = os.path.normpath(target['src_path'])

            # Try exact filename matches.
            result = self._targets_exact_match(targets, file_name)
            if result: return result

            # No exact match, try to find all targets with longest matching parent
            # directory.
            result = self._targets_longest_matches(targets, file_name)
            if result: return result

        # TODO: Alternatively, could run rustc directly without cargo.
        # rustc -Zno-trans -Zunstable-options --error-format=json file_name
        print('Rust Enhanced: Failed to find target for %r' % file_name)
        return []

    def _targets_manual_config(self, settings, file_name):
        """Check for Cargo targets in the Sublime settings."""
        # First check config for manual targets.
        for project in settings.get('projects', {}).values():
            src_root = os.path.join(project.get('root', ''), 'src')
            if not file_name.startswith(src_root):
                continue
            targets = project.get('targets', {})
            for tfile, tcmd in targets.items():
                if file_name == os.path.join(src_root, tfile):
                    return [(tfile, tcmd)]
            else:
                target = targets.get('_default', '')
                if target:
                    # Unfortunately don't have the target src filename.
                    return [('', target)]
        return None

    def _target_to_args(self, target):
        """Convert target from Cargo metadata to Cargo command-line argument."""
        # Targets have multiple "kinds" when you specify crate-type in
        # Cargo.toml, like:
        #   crate-type = ["rlib", "dylib"]
        #
        # Libraries are the only thing that support this at this time, and
        # generally you only use one command-line argument to build multiple
        # "kinds" (--lib in this case).
        #
        # Nightly beware:  [[example]] that specifies crate-type will no
        # longer show up as "example", making it impossible to compile.
        # See https://github.com/rust-lang/cargo/pull/3556 and
        # https://github.com/rust-lang/cargo/issues/3572
        #
        # For now, just grab the first kind since it will always result in the
        # same arguments.
        kind = target['kind'][0]
        if kind in ('lib', 'rlib', 'dylib', 'staticlib', 'proc-macro'):
            return (target['src_path'], '--lib')
        elif kind in ('bin', 'test', 'example', 'bench'):
            return (target['src_path'], '--'+kind+' '+target['name'])
        elif kind in ('custom-build',):
            # Could wait for "cargo check" or run rustc directly on the file.
            return None
        else:
            # Unknown kind, don't know how to build.
            raise ValueError(kind)

    def _targets_exact_match(self, targets, file_name):
        """Check for Cargo targets that exactly match the current file."""
        for target in targets:
            if target['src_path'] == file_name:
                args = self._target_to_args(target)
                if args:
                    return [args]
        return None

    def _targets_longest_matches(self, targets, file_name):
        """Determine the Cargo targets that are in the same directory (or
        parent) of the current file."""
        result = []
        # Find longest path match.
        # TODO: This is sub-optimal, because it may result in multiple targets.
        # Consider using the output of rustc --emit dep-info.
        # See https://github.com/rust-lang/cargo/issues/3211 for some possible
        # problems with that.
        path_match = os.path.dirname(file_name)
        found = False
        found_lib = False
        found_bin = False
        while not found:
            for target in targets:
                if os.path.dirname(target['src_path']) == path_match:
                    target_args = self._target_to_args(target)
                    if target_args:
                        result.append(target_args)
                        found = True
                        if target_args[1].startswith('--bin'):
                            found_bin = True
                        if target_args[1].startswith('--lib'):
                            found_lib = True
            p = os.path.dirname(path_match)
            if p == path_match:
                # Root path
                break
            path_match = p
        # If the match is both --bin and --lib in the same directory, just do --lib.
        if found_bin and found_lib:
            result = [x for x in result if not x[1].startswith('--bin')]
        return result

    def hide_phantoms(self, window):
        for view in window.views():
            view.erase_phantoms('rust-syntax-phantom')
            view.erase_regions('rust-invalid')
            view.erase_regions('rust-info')

    def add_error_phantoms(self, view_of_interest, info, settings,
        regions_by_view, target_src_path, parent_info):
        """Add messages to Sublime views.

        - `view_of_interest`: This is the view that triggered the syntax
          check.  If we receive any messages for this view, then
          this_view_found is set.
        - `info`: Dictionary of messages from rustc.
        - `settings`: Sublime settings.
        - `regions_by_view`: Dictionary used to map view to highlight regions (see above).
        - `target_src_path`: The path to the top-level Cargo target filename
          (like main.rs or lib.rs).
        - `parent_info`: Dictionary used for tracking "children" messages.
          Includes 'view' and 'region' keys to indicate where a child message
          should be displayed.
        """
        window = view_of_interest.window()
        error_colour = settings.get('rust_syntax_error_color', 'var(--redish)')
        warning_colour = settings.get('rust_syntax_warning_color', 'var(--yellowish)')

        # Include "notes" tied to errors, even if warnings are disabled.
        if (info['level'] != 'error' and
            settings.get('rust_syntax_hide_warnings', False) and
            not parent_info
           ):
            return

        # TODO: Consider matching the colors used by rustc.
        # - error: red
        #     `bug` appears as "error: internal compiler error"
        # - warning: yellow
        # - note: bright green
        # - help: cyan
        is_error = info['level'] == 'error'
        if is_error:
            base_color = error_colour
        else:
            base_color = warning_colour

        msg_template = """
            <body id="rust-message">
                <style>
                    span {{
                        font-family: monospace;
                    }}
                    .rust-error {{
                        color: %s;
                    }}
                    .rust-additional {{
                        color: var(--yellowish);
                    }}
                    a {{
                        text-decoration: inherit;
                        padding: 0.35rem 0.5rem 0.45rem 0.5rem;
                        position: relative;
                        font-weight: bold;
                    }}
                </style>
                <span class="{cls}">{level}: {msg} {extra}<a href="hide">\xD7</a></span>
            </body>""" % (base_color,)

        def click_handler(url):
            if url == 'hide':
                self.hide_phantoms(window)
            else:
                webbrowser.open_new(url)

        def add_message(view, region, message, extra=''):
            if view == view_of_interest:
                self.this_view_found = True
            vid = view.id()
            if vid not in regions_by_view:
                regions_by_view[vid] = {'view': view, 'regions': {}}
            # Unfortunately you cannot specify colors, but instead scopes as
            # defined in the color theme.  If the scope is not defined, then
            # it will show up as foreground color.  I just use "info" as an
            # undefined scope (empty string will remove regions).
            scope = 'invalid' if is_error else 'info'
            regions_by_view[vid]['regions'].setdefault(scope, []).append(region)

            # For some reason, with LAYOUT_BELOW, if you have a multi-line
            # region, the phantom is only displayed under the first line.  I
            # think it makes more sense for the phantom to appear below the
            # last line.
            start = view.rowcol(region.begin())
            end = view.rowcol(region.end())
            if start[0] != end[0]:
                # Spans multiple lines, adjust to the last line.
                region = sublime.Region(
                    view.text_point(end[0], 0),
                    region.end()
                )

            if info['level'] == 'error':
                cls = 'rust-error'
            else:
                cls = 'rust-additional'

            # Rust performs some pretty-printing for things like suggestions,
            # attempt to retain some of the formatting.  This isn't perfect
            # (doesn't line up perfectly), not sure why.
            message = html.escape(message, quote=False).\
                replace('\n', '<br>').replace(' ', '&nbsp;')
            content = msg_template.format(
                cls = cls,
                level = info['level'],
                msg = message,
                extra = extra
            )
            self._add_phantom(view,
                'rust-syntax-phantom', region,
                content,
                sublime.LAYOUT_BELOW,
                click_handler
            )

        def add_primary_message(view, region, message):
            parent_info['view'] = view
            parent_info['region'] = region
            # Not all codes have explanations (yet).
            if info['code'] and info['code']['explanation']:
                # TODO
                # This could potentially be a link that opens a Sublime popup, or
                # a new temp buffer with the contents of 'explanation'.
                # (maybe use sublime-markdown-popups)
                extra = ' <a href="https://doc.rust-lang.org/error-index.html#%s">?</a>' % (info['code']['code'],)
            else:
                extra = ''
            add_message(view, region, message, extra)

        def report_silent_message(path, message):
            print('rust: %s' % path)
            print('[%s]: %s' % (info['level'], message))

        if len(info['spans']) == 0:
            if parent_info:
                # This is extra info attached to the parent message.
                add_primary_message(parent_info['view'],
                                    parent_info['region'],
                                    info['message'])
            else:
                # Messages without spans are global session messages (like "main
                # function not found"). The most appropriate place for most of the
                # messages is the root path (like main.rs).
                #
                # Some of the messages are not very interesting, though.
                imsg = info['message']
                if not (imsg.startswith('aborting due to') or
                        imsg.startswith('cannot continue')):
                    view = window.find_open_file(os.path.realpath(target_src_path))
                    if view:
                        # Place at bottom of file for lack of anywhere better.
                        r = sublime.Region(view.size())
                        add_primary_message(view, r, imsg)
                    else:
                        report_silent_message(target_src_path, imsg)

        for span in info['spans']:
            is_primary = span['is_primary']
            if 'macros>' in span['file_name']:
                # Rust gives the chain of expansions for the macro, which we
                # don't really care about.  We want to find the site where the
                # macro was invoked.
                def find_span_r(span):
                    if 'macros>' in span['file_name']:
                        if span['expansion']:
                            return find_span_r(span['expansion']['span'])
                        else:
                            # XXX: Is this possible?
                            return None
                    else:
                        return span
                span = find_span_r(span)
                if span == None:
                    continue
            view = window.find_open_file(os.path.realpath(span['file_name']))
            if view:
                # Sublime text is 0 based whilst the line/column info from
                # rust is 1 based.
                region = sublime.Region(
                    view.text_point(span['line_start'] - 1, span['column_start'] - 1),
                    view.text_point(span['line_end'] - 1, span['column_end'] - 1)
                )

                label = span['label']
                if label:
                    # Display the label for this Span.
                    add_message(view, region, label)
                else:
                    # Some spans don't have a label.  These seem to just imply
                    # that the main "message" is sufficient, and always seems
                    # to happen with the span is_primary.
                    if not is_primary:
                        # When can this happen?
                        pprint(info)
                        raise ValueError('Unexpected span with no label')
                if is_primary:
                    # Show the overall error message.
                    add_primary_message(view, region, info['message'])
                if span['suggested_replacement']:
                    # The "suggested_replacement" contains the code that
                    # should replace the span.  However, it can be easier to
                    # read if you repeat the entire line (from "rendered").
                    add_message(view, region, info['rendered'])
            else:
                # File is currently not open.
                if is_primary:
                    report_silent_message(span['file_name'], info['message'])
                if span['label']:
                    report_silent_message(span['file_name'], span['label'])

        # Recurse into children (which typically hold notes).
        for child in info['children']:
            self.add_error_phantoms(view_of_interest, child, settings, regions_by_view, target_src_path, parent_info)

    def draw_region_highlights(self, regions_by_view):
        for d in regions_by_view.values():
            view = d['view']
            for scope, regions in d['regions'].items():
                # Is DRAW_EMPTY necessary?  Is it possible to have a zero-length span?
                self._add_regions(view, 'rust-%s' % scope, regions, scope, '',
                    sublime.DRAW_NO_FILL|sublime.DRAW_EMPTY)

    def _add_phantom(self, view, key, region, content, layout, on_navigate):
        """Pulled out to assist testing."""
        view.add_phantom(
            key, region,
            content,
            layout,
            on_navigate
        )

    def _add_regions(self, view, key, regions, scope, icon, flags):
        """Pulled out to assist testing."""
        view.add_regions(key, regions, scope, icon, flags)
