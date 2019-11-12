import io
import os
import re
import subprocess
from argparse import ArgumentParser
from collections import defaultdict, OrderedDict
from typing import Tuple, Dict

from django.apps import apps
from django.core.management import BaseCommand
from django.db.migrations import Migration
from django.utils.module_loading import import_string


class Command(BaseCommand):
    def add_arguments(self, parser: ArgumentParser):
        super().add_arguments(parser)
        parser.add_argument(
            "--include-squashed", "-S", default=False, action="store_true"
        )
        parser.add_argument(
            "--replace-squashed-dependencies", "-R", default=False, action="store_true"
        )
        parser.add_argument(
            "--remove-squashed-dependencies", "-D", default=False, action="store_true"
        )
        parser.add_argument("--graphviz", "-G", default=None)

    def handle(self, *args, **options):
        app_names = ()
        hide_squashed_nodes = not options["include_squashed"]
        replace_squashed_nodes = options["replace_squashed_dependencies"]
        remove_squashed_nodes = options["remove_squashed_dependencies"]
        graphviz_filename = options["graphviz"]

        edges = defaultdict(lambda: set())
        replaced_nodes = {}
        all_nodes = set()
        migrations = OrderedDict()  # type: Dict[Tuple[str, str], Migration]
        migration_paths = {}  # type: Dict[Tuple[str, str], str]
        squashing_migrations = set()

        # read all migrations
        for app_name in app_names:
            app_config = apps.get_app_config(app_name)
            migration_dir = os.path.join(app_config.path, "migrations")
            if not os.path.isdir(migration_dir):
                continue
            for filename in os.listdir(migration_dir):
                migration_abspath = os.path.join(migration_dir, filename)
                if (
                    filename == "__init__.py"
                    or not filename.endswith(".py")
                    or not os.path.isfile(migration_abspath)
                ):
                    continue
                migration_name = filename[:-3]
                dotted_path = "%s.migrations.%s.Migration" % (
                    app_config.models_module.__package__,
                    migration_name,
                )
                migration = import_string(dotted_path)  # type: Migration
                migrations[(app_name, migration_name)] = migration
                migration_paths[(app_name, migration_name)] = migration_abspath
                if migration.replaces:
                    squashing_migrations.add((app_name, migration_name))
                for replaced in migration.replaces:  # type: Tuple[str, str]
                    if not isinstance(replaced, tuple):
                        continue
                    elif replaced[0] in app_names:
                        replaced_nodes[replaced] = (app_name, migration_name)

        # build the graph
        def name(migration_tuple: Tuple[str, str]) -> str:
            return '("%s", "%s")' % migration_tuple

        for dst, migration in migrations.items():
            app_name, migration_name = dst
            if app_name not in app_names:
                continue
            for src in migration.dependencies:  # type: Tuple[str, str]
                if not isinstance(src, tuple):
                    continue
                original_src = src
                while src in replaced_nodes and hide_squashed_nodes:
                    src = replaced_nodes[src]
                if src != original_src:
                    if dst in replaced_nodes:
                        style = self.style.ERROR
                        prefix = "squashed "
                    else:
                        style = self.style.SUCCESS
                        prefix = ""
                    self.stdout.write(
                        style(
                            "migration %s replaced by %s in the dependencies of the %smigration %s"
                            % (name(original_src), name(src), prefix, name(dst))
                        )
                    )
                if src[0] in app_names:
                    edges[src].add(dst)
                    all_nodes.add(src)
            all_nodes.add(dst)

        # write the GraphViz file
        buffer = io.StringIO()
        buffer.write("digraph migrations {\n")

        def key(migration_tuple: Tuple[str, str]) -> str:
            return "%s_%s" % migration_tuple

        for src in sorted(all_nodes):
            if src in replaced_nodes and hide_squashed_nodes:
                continue
            dst_set = edges.get(src, set())
            color = "#79aec8"
            if any(dst[0] != src[0] for dst in dst_set if dst not in replaced_nodes):
                color = "#d9534f"
            label = "%s:%s" % src
            if src in squashing_migrations:
                label += " (squash)"
            if src in replaced_nodes:
                label += " (squashed)"
                color = "#eeeeee"
            buffer.write(
                '    %s [fillcolor="%s", style="filled", label="%s"];\n'
                % (key(src), color, label)
            )

        if not hide_squashed_nodes:
            for src, dst in replaced_nodes.items():
                buffer.write("    %s -> %s;\n" % (key(src), key(dst)))

        for src, dst_set in sorted(edges.items()):
            if src in replaced_nodes and hide_squashed_nodes:
                continue
            for dst in sorted(dst_set):
                if dst in replaced_nodes and hide_squashed_nodes:
                    continue
                buffer.write("    %s -> %s;\n" % (key(src), key(dst)))
        buffer.write("}\n")

        base, sep, ext = graphviz_filename.rpartition(".")
        if sep == "." and ext.lower() in ("png", "svg", "jpg"):
            with open(graphviz_filename, "wb") as out_fd:
                p = subprocess.Popen(
                    ["dot", "-T%s" % ext.lower()], stdout=out_fd, stdin=subprocess.PIPE
                )
                p.communicate(buffer.getvalue().encode())
        else:
            with open(graphviz_filename, "w") as out_fd:
                out_fd.write(buffer.getvalue())

        if replace_squashed_nodes:
            for migration_key, migration_path in migration_paths.items():
                migration = migrations[migration_key]
                with open(migration_path) as fd:
                    migration_content = fd.read()
                for src in migration.dependencies:
                    if not isinstance(src, tuple) or src not in replaced_nodes:
                        continue
                    dst_name = '("%s", "%s")' % replaced_nodes[src]
                    migration_content = re.sub(
                        r"\(\s*['\"]%s['\"]\s*,\s*['\"]%s['\"]\s*)" % src,
                        dst_name,
                        migration_content,
                    )
                with open(migration_path, "w") as fd:
                    fd.write(migration_content)
                self.stdout.write(self.style.SUCCESS("%s updated" % migration_path))

        if remove_squashed_nodes:
            for migration_key, migration_path in migration_paths.items():
                if migration_key in replaced_nodes:
                    os.remove(migration_path)
                    self.stdout.write(self.style.SUCCESS("%s deleted" % migration_path))
