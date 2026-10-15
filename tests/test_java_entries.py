"""Java entry discovery: main methods ranked by build and launch evidence."""
from pathlib import Path

from flowmap.project import load_project


def write(root: Path, relative: str, source: str):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)


MAIN = 'package a; public class {name} {{ public static void main(String[] args) {{ run(); }} static void run() {{}} }}'


def test_every_main_is_an_entry_but_tests_are_not(tmp_path):
    write(tmp_path, 'src/main/java/a/App.java', MAIN.format(name='App'))
    write(tmp_path, 'src/test/java/a/AppTest.java', 'package a; class AppTest { public static void main(String[] a) {} }')
    entries = load_project(tmp_path).entries
    assert [e.kind for e in entries] == ['java-main']
    assert entries[0].target.startswith('src/main/java/a/App.java::App.main@')
    assert entries[0].label == 'java a.App'


def test_varargs_main_counts_but_other_mains_do_not(tmp_path):
    write(tmp_path, 'A.java', 'class A { public static void main(String... args) {} }')
    write(tmp_path, 'B.java', 'class B { void main(String[] args) {} static void main(int x) {} }')
    assert [e.target.split('::')[1].split('@')[0] for e in load_project(tmp_path).entries] == ['A.main']


def test_pom_main_class_ranks_above_plain_main(tmp_path):
    write(tmp_path, 'pom.xml', '<project><build><plugins><plugin><artifactId>exec-maven-plugin</artifactId>'
          '<configuration><mainClass>a.App</mainClass></configuration></plugin></plugins></build></project>')
    write(tmp_path, 'src/main/java/a/App.java', MAIN.format(name='App'))
    write(tmp_path, 'src/main/java/a/Tool.java', MAIN.format(name='Tool'))
    entries = load_project(tmp_path).entries
    assert entries[0].kind == 'build-main' and '::App.main@' in entries[0].target
    assert any(s.text == 'pom.xml: <mainClass>a.App</mainClass>' for s in entries[0].sources)
    assert entries[1].kind == 'java-main' and '::Tool.main@' in entries[1].target


def test_spring_start_class_property(tmp_path):
    write(tmp_path, 'pom.xml', '<project><properties><start-class>a.App</start-class></properties></project>')
    write(tmp_path, 'src/main/java/a/App.java', MAIN.format(name='App'))
    assert load_project(tmp_path).entries[0].kind == 'build-main'


def test_gradle_application_main_class(tmp_path):
    write(tmp_path, 'app/build.gradle', "application { mainClass = 'a.App' }")
    write(tmp_path, 'app/src/main/java/a/App.java', MAIN.format(name='App'))
    assert load_project(tmp_path).entries[0].kind == 'build-main'


def test_gradle_kotlin_dsl_main_class(tmp_path):
    write(tmp_path, 'build.gradle.kts', 'application { mainClass.set("a.App") }')
    write(tmp_path, 'src/main/java/a/App.java', MAIN.format(name='App'))
    assert load_project(tmp_path).entries[0].kind == 'build-main'


def test_literal_java_commands(tmp_path):
    write(tmp_path, 'run.sh', 'java -cp build/classes a.App\njava -Xmx1g src/main/java/a/Tool.java\n')
    write(tmp_path, 'src/main/java/a/App.java', MAIN.format(name='App'))
    write(tmp_path, 'src/main/java/a/Tool.java', MAIN.format(name='Tool'))
    kinds = {e.target.split('::')[1].split('@')[0]: e.kind for e in load_project(tmp_path).entries}
    assert kinds == {'App.main': 'shell', 'Tool.main': 'shell'}


def test_java_jar_uses_the_declared_build_main_class(tmp_path):
    write(tmp_path, 'pom.xml', '<project><properties><start-class>a.App</start-class></properties></project>')
    write(tmp_path, 'Dockerfile', 'FROM eclipse-temurin\nCMD ["java", "-jar", "app.jar"]\n')
    write(tmp_path, 'src/main/java/a/App.java', MAIN.format(name='App'))
    entry = load_project(tmp_path).entries[0]
    assert entry.kind == 'docker' and '::App.main@' in entry.target


def test_no_entry_for_missing_or_ambiguous_main_class(tmp_path):
    write(tmp_path, 'pom.xml', '<project><build><plugins><plugin><configuration><mainClass>${main}</mainClass>'
          '</configuration></plugin></plugins></build></project>')
    write(tmp_path, 'run.sh', 'java -cp x b.Missing\n')
    write(tmp_path, 'src/main/java/a/Lib.java', 'package a; public class Lib { void main(String[] a) {} }')
    assert load_project(tmp_path).entries == []


def test_commented_build_declarations_are_ignored(tmp_path):
    write(tmp_path, 'pom.xml', '<project><!-- <mainClass>a.Old</mainClass> --><properties><start-class>a.App</start-class></properties></project>')
    write(tmp_path, 'build.gradle', "// mainClass = 'a.Old'\n/* mainClass = 'a.Old' */\n")
    write(tmp_path, 'src/main/java/a/App.java', MAIN.format(name='App'))
    write(tmp_path, 'src/main/java/a/Old.java', MAIN.format(name='Old'))
    kinds = {e.label: e.kind for e in load_project(tmp_path).entries}
    assert kinds == {'java a.App': 'build-main', 'java a.Old': 'java-main'}


def test_java_jar_ignores_exec_plugin_main_classes(tmp_path):
    write(tmp_path, 'pom.xml', '<project><build><plugins><plugin><artifactId>exec-maven-plugin</artifactId>'
          '<configuration><mainClass>a.Tool</mainClass></configuration></plugin></plugins></build></project>')
    write(tmp_path, 'Dockerfile', 'FROM x\nCMD ["java", "-jar", "app.jar"]\n')
    write(tmp_path, 'src/main/java/a/Tool.java', MAIN.format(name='Tool'))
    assert [e.kind for e in load_project(tmp_path).entries] == ['build-main']


def test_jar_plugin_manifest_main_class_maps_java_jar(tmp_path):
    write(tmp_path, 'pom.xml', '<project><build><plugins><plugin><artifactId>maven-jar-plugin</artifactId>'
          '<configuration><archive><manifest><mainClass>a.App</mainClass></manifest></archive></configuration>'
          '</plugin></plugins></build></project>')
    write(tmp_path, 'Dockerfile', 'FROM x\nCMD ["java", "-jar", "app.jar"]\n')
    write(tmp_path, 'src/main/java/a/App.java', MAIN.format(name='App'))
    assert load_project(tmp_path).entries[0].kind == 'docker'


def test_test_source_sets_main_signatures_and_binary_names(tmp_path):
    write(tmp_path, 'src/integrationTest/java/a/It.java', MAIN.format(name='It'))
    write(tmp_path, 'src/testFixtures/java/a/Fx.java', MAIN.format(name='Fx'))
    write(tmp_path, 'src/main/java/a/W.java', 'package a; class W { static int main(String[] a) { return 0; } }')
    write(tmp_path, 'src/main/java/a/Q.java', 'package a; class Q { public static void main(java.lang.String[] a) {} }')
    write(tmp_path, 'src/main/java/a/Outer.java', 'package a; class Outer { static class Inner { public static void main(String[] a) {} } }')
    # Exec-form CMD has no shell expansion; a shell script's $Inner would be a variable.
    write(tmp_path, 'Dockerfile', 'FROM x\nCMD ["java", "-cp", "out", "a.Outer$Inner"]\n')
    entries = {e.label: e.kind for e in load_project(tmp_path).entries}
    assert entries == {'java a.Q': 'java-main', 'java a.Outer.Inner': 'docker'}


def test_lambdas_with_inferred_parameters_do_not_break_entry_discovery(tmp_path):
    write(tmp_path, 'src/main/java/a/App.java', 'package a; public class App { public static void main(String[] args) '
          '{ java.util.function.Function<String, String> f = x -> x; f.apply("a"); } }')
    assert [e.label for e in load_project(tmp_path).entries] == ['java a.App']
