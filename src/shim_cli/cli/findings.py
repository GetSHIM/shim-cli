"""Findings in shim's published schema v1 (docs/schemas/finding-v1.json).

Every text field comes from a fixed template filled with the client's name and
the finding's own numbers. A path, a value, a command line, a tool or model name
the client sent, or a check's detail never reaches a finding.
"""

from __future__ import annotations

import datetime
import uuid
from typing import NamedTuple

from shim_cli.events.injection import MARKERS
from shim_cli.guard import ENTITY_TYPES
from shim_cli.session import summary
from shim_cli.watch.measure import TRUNCATED

NAMESPACE = uuid.UUID("753f9b9f-aab5-47d4-9ca6-bb5893974601")
LARGE_TOOL_RESULT_BYTES = 25_000
MAX_EVIDENCE = 20
# No hook event holds more: it reads at most a million characters
# (events.payload.MAX_TEXT_CHARACTERS), so a larger count is a corrupt record.
MAX_COUNT = 1_000_000
NAMES = {
    "claude": "Claude Code",
    "codex": "Codex",
    "copilot": "GitHub Copilot CLI",
    "vscode": "VS Code",
}
OTHER = "other"
_DOCTOR_PLAYBOOK = "docs/commands.md#error-codes"
_INSTALL = "shim install {client} --yes"
_INSTALL_TR = "shim install {client} --yes komutunu çalıştırın"


class Rule(NamedTuple):
    title: str
    summary: tuple[str, str]
    text: tuple[str, str]
    severity: str = "low"
    risk_class: str = "reliability"
    command: str | None = None
    reversible: bool = True
    playbook: str = _DOCTOR_PLAYBOOK


def _playbook(rule_id: str) -> str:
    return "docs/findings.md#" + rule_id.replace(".", "")


# A doctor finding is `high` when its check failed; `severity` is its WARN level.
DOCTOR = {
    "CLIENT_NOT_FOUND": Rule(
        "Client not found on PATH",
        (
            "{name} is not on PATH, so shim cannot check it.",
            "{name} PATH üzerinde bulunamadı, shim onu denetleyemiyor.",
        ),
        (
            "Install {name}, or add the folder that holds {client} to PATH.",
            "{name} uygulamasını kurun ya da {client} komutunun bulunduğu klasörü "
            "PATH'e ekleyin.",
        ),
    ),
    "CLIENT_VERSION_UNKNOWN": Rule(
        "Client version unreadable",
        (
            "{name} did not report a version shim can read.",
            "{name} shim'in okuyabildiği bir sürüm bildirmedi.",
        ),
        (
            "Run {client} --version; reinstall {name} if it fails.",
            "{client} --version komutunu çalıştırın; hata verirse {name} "
            "uygulamasını yeniden kurun.",
        ),
    ),
    "CLIENT_TOO_OLD": Rule(
        "Client older than supported",
        (
            "{name} is older than the oldest version shim supports.",
            "{name}, shim'in desteklediği en eski sürümden daha eski.",
        ),
        (
            "Update {name} to the version shim doctor {client} names.",
            "{name} uygulamasını shim doctor {client} çıktısının belirttiği sürüme "
            "güncelleyin.",
        ),
    ),
    "CLIENT_NEWER_THAN_TESTED": Rule(
        "Client newer than tested",
        (
            "{name} is newer than the version shim was tested against.",
            "{name}, shim'in test edildiği sürümden daha yeni.",
        ),
        (
            "Check shim once by hand: a prompt holding a test e-mail address must "
            "be reported.",
            "shim'i bir kez elle deneyin: test e-posta adresi içeren bir istem "
            "raporlanmalı.",
        ),
    ),
    "CODEX_HOOKS_DISABLED": Rule(
        "Codex hooks disabled",
        (
            "Codex reports hooks as off, so no prompt reaches shim.",
            "Codex kancaları kapalı gösteriyor; hiçbir istem shim'e ulaşmıyor.",
        ),
        (
            "Set hooks = true under [features] in Codex's config.toml, or delete "
            "that line.",
            "Codex'in config.toml dosyasında [features] altında hooks = true yapın "
            "ya da o satırı silin.",
        ),
        risk_class="privacy",
    ),
    "CODEX_HOOKS_UNCHECKED": Rule(
        "Codex hook support unchecked",
        (
            "codex features list could not be run, so shim cannot tell whether "
            "hooks are on.",
            "codex features list çalıştırılamadı; shim kancaların açık olup "
            "olmadığını bilemiyor.",
        ),
        (
            "Run codex features list; it must show hooks as true.",
            "codex features list komutunu çalıştırın; hooks değeri true görünmeli.",
        ),
    ),
    "LEGACY_NAMES_PRESENT": Rule(
        "0.2.0 names left on disk",
        (
            "A 0.2.0 hook, settings file or ledger is still on disk for {name}.",
            "{name} için 0.2.0 sürümünden kalan bir kanca, ayar dosyası ya da kayıt "
            "defteri hâlâ diskte.",
        ),
        (
            "Run the shim install, shim config or shim report command that shim "
            "doctor {client} names; each moves or replaces one leftover.",
            "shim doctor {client} çıktısının belirttiği shim install, shim config "
            "ya da shim report komutunu çalıştırın; her biri kalan bir öğeyi taşır "
            "ya da değiştirir.",
        ),
    ),
    "HOOK_NOT_INSTALLED": Rule(
        "Hook not installed",
        (
            "The {name} settings file does not hold exactly this shim "
            "installation's hook; without any shim hook there, nothing is inspected.",
            "{name} ayar dosyasında tam olarak bu shim kurulumunun kancası yok; "
            "orada hiç shim kancası yoksa hiçbir şey denetlenmiyor.",
        ),
        (
            f"Run {_INSTALL}, then start a new {{name}} session.",
            f"{_INSTALL_TR}, ardından yeni bir {{name}} oturumu başlatın.",
        ),
        risk_class="privacy",
        command=_INSTALL,
    ),
    "DETECTION_DISABLED": Rule(
        "Detection disabled",
        (
            "Every entity type is turned off, so shim finds nothing.",
            "Tüm veri türleri kapalı; shim hiçbir şey bulmuyor.",
        ),
        (
            "Turn the types you need back on with shim config --enable ENTITY "
            "--yes; shim config lists them.",
            "Gereken türleri shim config --enable ENTITY --yes ile yeniden açın; "
            "shim config hepsini listeler.",
        ),
        risk_class="privacy",
    ),
    "CUSTOM_PATTERN_UNSAFE": Rule(
        "Custom pattern backtracks",
        (
            "A custom pattern in shim's settings backtracks and could overrun the "
            "hook's deadline.",
            "shim ayarlarındaki özel bir desen geri izleme yapıyor ve kancanın süre "
            "sınırını aşabilir.",
        ),
        (
            "Simplify the pattern, or remove it with shim config --remove-custom "
            "NAME --yes.",
            "Deseni sadeleştirin ya da shim config --remove-custom NAME --yes ile "
            "kaldırın.",
        ),
    ),
    "HOOK_RUNNER_FAILED": Rule(
        "Hook runner failed its fixtures",
        (
            "The hook command shim install wrote did not pass its own safe and "
            "sensitive fixtures. A hook in the client's settings has no launcher, "
            "so the client decides what a failure does and may let prompts and "
            "tool calls through uninspected.",
            "shim install'ın yazdığı kanca komutu kendi güvenli ve hassas test "
            "girdilerini geçemedi. İstemcinin ayarlarındaki kancanın başlatıcısı "
            "yok; hatada ne olacağına istemci karar verir ve istemleri ile araç "
            "çağrılarını denetlenmeden geçirebilir.",
        ),
        (
            "Reinstall shim: uv tool install --reinstall shim, or pipx reinstall shim.",
            "shim'i yeniden kurun: uv tool install --reinstall shim ya da pipx "
            "reinstall shim.",
        ),
        risk_class="privacy",
    ),
    "HOOK_RESOLUTION_FAILED": Rule(
        "No runnable hook",
        (
            "No shim hook is runnable for {name} from this installation. A hook "
            "in the client's settings that cannot run may let prompts through "
            "uninspected; the plugin's launcher withholds them instead.",
            "{name} için bu kurulumdan çalıştırılabilir bir shim kancası yok. "
            "İstemci ayarlarındaki çalışamayan bir kanca istemleri denetlenmeden "
            "geçirebilir; eklentinin başlatıcısı ise onları alıkoyar.",
        ),
        (f"Run {_INSTALL}.", f"{_INSTALL_TR}."),
        risk_class="privacy",
        command=_INSTALL,
    ),
    "SETTINGS_FILE_ABSENT": Rule(
        "No settings file",
        (
            "shim has no settings file, so the shipped defaults apply.",
            "shim'in ayar dosyası yok; varsayılanlar geçerli.",
        ),
        (
            "Nothing is wrong if the defaults suit you; the first change you save "
            "with shim config creates the file.",
            "Varsayılanlar işinize yarıyorsa sorun yok; shim config ile kaydettiğiniz "
            "ilk değişiklik dosyayı oluşturur.",
        ),
        severity="informational",
    ),
    "CUSTOM_PATTERNS_INACTIVE": Rule(
        "Custom patterns switched off",
        (
            "Custom patterns are in shim's settings but the CUSTOM type is off, so "
            "none of them runs.",
            "Özel desenler shim ayarlarında duruyor ama CUSTOM türü kapalı; hiçbiri "
            "çalışmıyor.",
        ),
        (
            "Turn them on with shim config --enable CUSTOM --yes.",
            "shim config --enable CUSTOM --yes ile açın.",
        ),
        risk_class="privacy",
        command="shim config --enable CUSTOM --yes",
    ),
    "MODE_DEFAULT_WEAKENS": Rule(
        "Mode default overrides masking",
        (
            "The [mode] default in shim's settings overrides the shipped enforce "
            "for tool results and for tool inputs that leave this machine, so they "
            "are not masked.",
            "shim ayarlarındaki [mode] default, araç sonuçları ve bu makineden "
            "çıkan araç girdileri için varsayılan enforce'u geçersiz kılıyor; "
            "bunlar maskelenmiyor.",
        ),
        (
            'Add inbound = "enforce" and outbound = "enforce" under [mode], or '
            "remove default.",
            '[mode] altına inbound = "enforce" ve outbound = "enforce" ekleyin ya '
            "da default satırını kaldırın.",
        ),
        risk_class="privacy",
    ),
    "ARCHIVE_VERSION_SKEW": Rule(
        "Plugin archive and package differ",
        (
            "The plugin's bundled hook archive and the shim package are different "
            "versions.",
            "Eklentinin paketlenmiş kanca arşivi ile shim paketi farklı sürümlerde.",
        ),
        (
            "Run claude plugin update shim-cli@shim-cli and restart Claude Code; "
            "in a cloned plugin folder, check out the newest release tag.",
            "claude plugin update shim-cli@shim-cli komutunu çalıştırıp Claude "
            "Code'u yeniden başlatın; klonlanmış eklenti klasöründe en yeni sürüm "
            "etiketine geçin.",
        ),
    ),
    "PLUGIN_NOT_DISCOVERABLE": Rule(
        "Plugin install not discoverable",
        (
            "shim cannot see whether {name} also has the shim plugin installed.",
            "shim, {name} için shim eklentisinin de kurulu olup olmadığını göremiyor.",
        ),
        (
            "If the {name} plugin is installed beside shim install {client}, "
            "remove the plugin.",
            "{name} eklentisi shim install {client} ile birlikte kuruluysa "
            "eklentiyi kaldırın.",
        ),
        severity="informational",
    ),
    "DUPLICATE_HOOKS": Rule(
        "shim registered twice",
        (
            "shim is registered twice for {name}, so every event is inspected twice.",
            "shim, {name} için iki kez kayıtlı; her olay iki kez denetleniyor.",
        ),
        (
            "Keep one, as shim doctor {client} names: shim install {client} --yes "
            "leaves a single settings hook; beside the plugin, uninstall the plugin "
            "or remove the settings hook with shim revert {client} --yes.",
            "Birini bırakın, shim doctor {client} çıktısının belirttiği gibi: shim "
            "install {client} --yes tek bir ayar kancası bırakır; eklenti de "
            "kuruluysa eklentiyi kaldırın ya da ayar kancasını shim revert {client} "
            "--yes ile kaldırın.",
        ),
        risk_class="privacy",
    ),
    "SESSION_RECORDS_UNWRITABLE": Rule(
        "Session records unwritable",
        (
            "Session records cannot be written; masking still works but no "
            "summary appears.",
            "Oturum kayıtları yazılamıyor; maskeleme çalışıyor ama özet görünmüyor.",
        ),
        (
            "Set TMPDIR to a folder you own, or make the shim-session folder in it "
            "yours with mode 700.",
            "TMPDIR'i size ait bir klasöre ayarlayın ya da içindeki shim-session "
            "klasörünü 700 izniyle size ait yapın.",
        ),
    ),
    "HOOK_EVENTS_MISSING": Rule(
        "Hook events missing",
        (
            "Fewer {name} events are installed than shim can cover.",
            "shim'in kapsayabileceğinden daha az {name} olayı kurulu.",
        ),
        (
            f"Run {_INSTALL}, then start a new {{name}} session.",
            f"{_INSTALL_TR}, ardından yeni bir {{name}} oturumu başlatın.",
        ),
        risk_class="privacy",
        command=_INSTALL,
    ),
    "HOOK_ACTIVATION_UNVERIFIED": Rule(
        "Hook activation unverified",
        (
            "Whether {name} runs the hook is client state shim cannot read.",
            "{name} uygulamasının kancayı çalıştırıp çalıştırmadığı, shim'in "
            "okuyamadığı bir istemci durumu.",
        ),
        (
            "Open /hooks in {name} and check that shim is listed (in Codex, also "
            "enabled).",
            "{name} içinde /hooks açın ve shim'in listelendiğini (Codex'te ayrıca "
            "etkin olduğunu) kontrol edin.",
        ),
        severity="informational",
    ),
    "SETTINGS_INVALID": Rule(
        "shim settings invalid",
        (
            "shim's settings file does not parse or holds a value shim does not "
            "accept, so every prompt is withheld.",
            "shim ayar dosyası ayrıştırılamıyor ya da kabul edilmeyen bir değer "
            "içeriyor; her istem bekletiliyor.",
        ),
        (
            "Edit the line of the settings file that shim doctor {client} names "
            "until it parses. As a last resort, shim config --reset --yes starts "
            "over and discards every setting.",
            "Ayar dosyasında shim doctor {client} çıktısının belirttiği satırı, "
            "dosya ayrıştırılana kadar düzeltin. Son çare olarak shim config "
            "--reset --yes baştan başlar ve tüm ayarları siler.",
        ),
        risk_class="privacy",
    ),
    "SETTINGS_REFUSED": Rule(
        "shim settings refused",
        (
            "shim's settings file was refused as unsafe, so every prompt is withheld.",
            "shim ayar dosyası güvensiz bulunarak reddedildi; her istem bekletiliyor.",
        ),
        (
            "Make the settings file a regular file you own, mode 600, in a folder "
            "only you can write to; the fix in shim doctor {client} --json names "
            "the exact command.",
            "Ayar dosyasını size ait, 600 izinli ve yalnızca sizin yazabildiğiniz "
            "bir klasörde duran normal bir dosya yapın; shim doctor {client} --json "
            "çıktısındaki fix tam komutu verir.",
        ),
        risk_class="privacy",
    ),
    "CLIENT_SETTINGS_UNREADABLE": Rule(
        "Client settings unreadable",
        (
            "The {name} settings file could not be inspected.",
            "{name} ayar dosyası incelenemedi.",
        ),
        (
            "Make the {name} settings file and its folder readable by you.",
            "{name} ayar dosyasını ve klasörünü sizin okuyabileceğiniz hâle getirin.",
        ),
    ),
    "CLIENT_SETTINGS_MALFORMED": Rule(
        "Client settings malformed",
        (
            "The {name} settings file does not parse, so shim will not change it.",
            "{name} ayar dosyası ayrıştırılamıyor; shim onu değiştirmeyecek.",
        ),
        (
            "Fix the {name} settings file by hand so it parses; shim install will "
            "not change it until then.",
            "{name} ayar dosyasını ayrıştırılabilir olacak şekilde elle düzeltin; o "
            "zamana kadar shim install onu değiştirmez.",
        ),
    ),
    "CLIENT_SETTINGS_UNSAFE": Rule(
        "Client settings unsafe",
        (
            "The {name} settings file is a link, someone else's, or otherwise unsafe.",
            "{name} ayar dosyası bir bağlantı, başkasına ait ya da başka bir "
            "nedenle güvensiz.",
        ),
        (
            f"Make the {{name}} settings file a regular file owned by you, then run "
            f"{_INSTALL}.",
            f"{{name}} ayar dosyasını size ait normal bir dosya yapın, ardından "
            f"{_INSTALL_TR}.",
        ),
    ),
}

SESSION = {
    "cli.session.unmasked_values": Rule(
        "Values reached the model unmasked",
        (
            "{events} failed tool call(s) in this {name} session printed values "
            "shim could not mask, so the model saw them.",
            "Bu {name} oturumunda {events} başarısız araç çağrısı shim'in "
            "maskeleyemediği değerler yazdırdı; model bunları gördü.",
        ),
        (
            "Rotate any credential the failed call printed: it reached the model "
            "provider. Point the agent at shim keys instead of cat for .env files, "
            "so a failing command cannot print a value.",
            "Başarısız çağrının yazdırdığı kimlik bilgilerini yenileyin: model "
            "sağlayıcısına ulaştı. .env dosyaları için ajanı cat yerine shim keys "
            "komutuna yönlendirin; böylece başarısız bir komut değer yazdıramaz.",
        ),
        "medium",
        "privacy",
        reversible=False,
        playbook=_playbook("cli.session.unmasked_values"),
    ),
    "cli.session.not_inspected": Rule(
        "Tool events shim could not inspect",
        (
            "In this {name} session shim could not inspect {not_inspected} "
            "event(s) and inspected {incomplete} only in part.",
            "Bu {name} oturumunda shim {not_inspected} olayı denetleyemedi, "
            "{incomplete} olayı yalnızca kısmen denetleyebildi.",
        ),
        (
            "Run shim report to see which tool or file it was, and shim doctor "
            "{client} to check the install.",
            "Hangi araç ya da dosya olduğunu görmek için shim report, kurulumu "
            "denetlemek için shim doctor {client} komutunu çalıştırın.",
        ),
        "medium",
        "privacy",
        playbook=_playbook("cli.session.not_inspected"),
    ),
    "cli.session.injection_markers": Rule(
        "Tool results read as instructions",
        (
            "Tool results in this {name} session held text that reads as "
            "instructions to the model; shim report names the files.",
            "Bu {name} oturumunda araç sonuçlarında modele talimat gibi okunan "
            "metin vardı; shim report dosyaları adlandırır.",
        ),
        (
            "Read the files shim report names and remove instructions you did not "
            'write. To have the model told, set markers = "note" in shim\'s '
            "settings file.",
            "shim report çıktısının belirttiği dosyaları okuyun ve sizin yazmadığınız "
            "talimatları kaldırın. Modelin de uyarılması için shim ayar dosyasında "
            'markers = "note" ayarlayın.',
        ),
        "low",
        "privacy",
        playbook=_playbook("cli.session.injection_markers"),
    ),
}

WATCH = {
    "cli.watch.cache_rewrite": Rule(
        "Cached prefix written again",
        (
            "{requests} request(s) wrote again a prefix the previous one had cached "
            "(~{tokens} tokens written again).",
            "{requests} istek, bir önceki isteğin önbelleğe aldığı ön eki yeniden "
            "yazdı (~{tokens} token yeniden yazıldı).",
        ),
        (
            "Keep CLAUDE.md and the tool list stable within a session: a change "
            "early in the prompt makes the next request write the whole cached "
            "prefix again.",
            "Bir oturum içinde CLAUDE.md dosyasını ve araç listesini sabit tutun: "
            "istemin başındaki bir değişiklik, sonraki isteğin önbellekteki ön ekin "
            "tamamını yeniden yazmasına yol açar.",
        ),
        "low",
        "cost",
        playbook=_playbook("cli.watch.cache_rewrite"),
    ),
    "cli.watch.duplicate_in_flight": Rule(
        "Identical request sent while one was in flight",
        (
            "{count} request(s) were sent while an identical one was still in "
            "flight (~{input_tokens} input tokens billed again).",
            "{count} istek, aynısı hâlâ yanıt beklerken gönderildi "
            "(~{input_tokens} girdi tokenı yeniden faturalandı).",
        ),
        (
            "Look for a client retry or a parallel agent sending the same request; "
            "shim watch forwards both and never merges them.",
            "Aynı isteği gönderen bir istemci yeniden denemesi ya da paralel bir "
            "ajan arayın; shim watch ikisini de iletir ve asla birleştirmez.",
        ),
        "low",
        "cost",
        playbook=_playbook("cli.watch.duplicate_in_flight"),
    ),
    "cli.watch.output_cut_off": Rule(
        "Response stopped at the output limit",
        (
            "{responses} of {requests} response(s) stopped at the output limit.",
            "{requests} yanıttan {responses} tanesi çıktı sınırında kesildi.",
        ),
        (
            "Raise the output limit (CLAUDE_CODE_MAX_OUTPUT_TOKENS in Claude Code) "
            "or ask for a shorter answer; a cut-off response is incomplete.",
            "Çıktı sınırını yükseltin (Claude Code'da CLAUDE_CODE_MAX_OUTPUT_TOKENS) "
            "ya da daha kısa bir yanıt isteyin; kesilen yanıt eksiktir.",
        ),
        "low",
        "quality",
        playbook=_playbook("cli.watch.output_cut_off"),
    ),
    "cli.watch.image_tokens": Rule(
        "Images in the requests",
        (
            "{distinct} distinct image(s) cost ~{estimated_tokens} input tokens "
            "across {requests} request(s) (estimated).",
            "{distinct} farklı görsel, {requests} istekte yaklaşık "
            "{estimated_tokens} girdi tokenı tuttu (tahmini).",
        ),
        (
            "Crop or shrink screenshots before attaching them; history replays "
            "every image in each later request.",
            "Ekran görüntülerini eklemeden önce kırpın ya da küçültün; geçmiş her "
            "görseli sonraki her istekte yeniden gönderir.",
        ),
        "informational",
        "cost",
        playbook=_playbook("cli.watch.image_tokens"),
    ),
    "cli.watch.large_tool_result": Rule(
        "Large tool result",
        (
            "The largest tool result was {bytes} bytes (~{estimated_tokens} "
            "tokens), carried by {requests} request(s).",
            "En büyük araç sonucu {bytes} bayttı (~{estimated_tokens} token) ve "
            "{requests} istekte taşındı.",
        ),
        (
            "Ask for less output: filter command output with head, grep or a "
            "narrower query, or write it to a file the agent reads in parts.",
            "Daha az çıktı isteyin: komut çıktısını head, grep ya da daha dar bir "
            "sorguyla süzün veya ajanın parça parça okuduğu bir dosyaya yazın.",
        ),
        "informational",
        "cost",
        playbook=_playbook("cli.watch.large_tool_result"),
    ),
}

RULES = {
    **{f"cli.doctor.{code.lower()}": rule for code, rule in DOCTOR.items()},
    **SESSION,
    **WATCH,
}


def _utc(moment: datetime.datetime) -> str:
    moment = moment.astimezone(datetime.timezone.utc).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def _parsed(stamp: object) -> datetime.datetime | None:
    """The moment a record names, in UTC; None without an offset or a reading."""
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if moment.utcoffset() is None:
            return None
        return moment.astimezone(datetime.timezone.utc)
    except (ValueError, OverflowError):
        return None


def _finding(
    rule_id: str,
    subject: dict,
    window: tuple[str, str],
    occurrences: int | None,
    evidence: list[str],
    measurements: dict,
    impact: dict,
    *,
    client: str = "claude",
    severity: str | None = None,
) -> dict:
    rule = RULES[rule_id]
    fields = {"client": client, "name": NAMES.get(client, client), **measurements}
    command = rule.command and rule.command.format(**fields)
    return {
        "schema_version": "1",
        "id": str(
            uuid.uuid5(NAMESPACE, f"{rule_id}|{subject['kind']}|{subject['id']}")
        ),
        "source": "shim-cli",
        "rule_id": rule_id,
        "rule_version": 1,
        "title": rule.title,
        "summary": {
            "en": rule.summary[0].format(**fields),
            "tr": rule.summary[1].format(**fields),
        },
        "severity": severity or rule.severity,
        "status": "open",
        "status_detail": None,
        "subject": subject,
        "window": {"start": window[0], "end": window[1]},
        "occurrences": occurrences,
        "evidence": [
            {"kind": "cli_session", "id": reference}
            for reference in evidence[:MAX_EVIDENCE]
        ],
        "measurements": measurements,
        "impact": {
            "requests": None,
            "tokens": None,
            "usd": None,
            "risk_class": rule.risk_class,
            **impact,
        },
        "remediation": {
            "mode": "suggest",
            "max_mode": "suggest",
            "action": (
                {"kind": "cli.command", "params": {"command": command}}
                if command
                else None
            ),
            "reversible": rule.reversible,
            "blast_radius": "app",
            "proof_after": None,
            "text": {
                "en": rule.text[0].format(**fields),
                "tr": rule.text[1].format(**fields),
            },
        },
        "playbook": rule.playbook,
    }


def from_checks(client: str, checks: list, now: datetime.datetime) -> list[dict]:
    """One finding per doctor check that did not pass."""
    moment = _utc(now)
    return [
        _finding(
            f"cli.doctor.{check.code.lower()}",
            {"kind": "app", "id": client},
            (moment, moment),
            1,
            [f"doctor:{check.name}"],
            {},
            {},
            client=client,
            severity="high" if check.status == "FAIL" else None,
        )
        for check in checks
        if check.status != "PASS"
    ]


def _counted(records: list, key: str, names: tuple, prefix: str) -> dict:
    """Counts by name, for names of shim's own vocabulary only. A count no
    event can hold is skipped."""
    counts: dict = {}
    for record in records:
        found = record.get(key)
        if isinstance(found, dict):
            pairs = found.items()
        elif isinstance(found, list):
            pairs = [(name, 1) for name in found]
        else:
            continue
        for name, count in pairs:
            if name in names and type(count) is int and 0 < count <= MAX_COUNT:
                measure = f"{prefix}{name.lower()}"
                counts[measure] = counts.get(measure, 0) + count
    return counts


def from_records(records: list, now: datetime.datetime) -> list[dict]:
    """Findings for one session's records, in spool order. `now` stands in for
    the window only when no qualifying record carries a readable timestamp."""
    ordinals = {id(record): ordinal for ordinal, record in enumerate(records, 1)}
    records = [record for record in records if isinstance(record, dict)]
    found = []

    def add(rule_id: str, chosen: list, measurements: dict, impact: dict) -> None:
        if not chosen:
            return
        moments = sorted(
            moment
            for record in chosen
            if (moment := _parsed(record.get("ts"))) is not None
        ) or [now]
        client = chosen[0].get("client")
        client = client if isinstance(client, str) and client in NAMES else OTHER
        found.append(
            _finding(
                rule_id,
                {"kind": "app", "id": client},
                (_utc(moments[0]), _utc(moments[-1])),
                len(chosen),
                [f"report:record:{ordinals[id(record)]}" for record in chosen],
                measurements,
                impact,
                client=client,
            )
        )

    acted = summary.acted_on(records)
    unmasked = [record for record in acted if summary.action_of(record) == "unmasked"]
    add(
        "cli.session.unmasked_values",
        unmasked,
        {
            "events": len(unmasked),
            **_counted(unmasked, "entities", ENTITY_TYPES, "entities_"),
        },
        {"requests": len(unmasked)},
    )
    skipped = summary.uninspected(records)
    partial = summary.incomplete(records)
    either = {id(record) for record in skipped + partial}
    add(
        "cli.session.not_inspected",
        [record for record in records if id(record) in either],
        {"not_inspected": len(skipped), "incomplete": len(partial)},
        {},
    )
    markers = [
        record for record in records if _counted([record], "markers", MARKERS, "")
    ]
    add(
        "cli.session.injection_markers",
        markers,
        _counted(markers, "markers", MARKERS, "markers_"),
        {},
    )
    return found


def _usd(amount: float | None) -> str | None:
    return None if amount is None else f"{amount:.6f}"


def from_watch(
    document: dict, started: datetime.datetime, ended: datetime.datetime
) -> list[dict]:
    """Findings for the waste lines of one `shim watch` report."""
    window = (_utc(started), _utc(ended))
    exchanges = document["exchanges"]
    app = {"kind": "app", "id": "claude"}
    found = []

    def references(indexes) -> list[str]:
        return [f"watch:exchange:{index}" for index in indexes]

    def by_model(indexes) -> dict:
        groups: dict = {}
        for index in indexes:
            model = exchanges[index - 1]["priced_as"] or OTHER
            groups.setdefault(model, []).append(index)
        return groups

    rewrites = document["cache_rewrites"]
    groups = by_model(rewrites["requests"])
    for model, indexes in groups.items():
        tokens = sum(
            exchanges[index - 1]["usage"]["cache_creation_input_tokens"]
            for index in indexes
        )
        # ponytail: the session total is one model's only when one model rewrote.
        usd = rewrites["spend_usd"] if len(groups) == 1 else None
        found.append(
            _finding(
                "cli.watch.cache_rewrite",
                {"kind": "model", "id": model},
                window,
                len(indexes),
                references(indexes),
                {"requests": len(indexes), "tokens": tokens},
                {"tokens": tokens, "usd": _usd(usd)},
            )
        )

    duplicates = document["duplicates_in_flight"]
    if duplicates["count"]:
        indexes = [
            index
            for index, exchange in enumerate(exchanges, 1)
            if exchange["duplicate_in_flight"]
        ]
        found.append(
            _finding(
                "cli.watch.duplicate_in_flight",
                app,
                window,
                duplicates["count"],
                references(indexes),
                {
                    "count": duplicates["count"],
                    "input_tokens": duplicates["input_tokens"],
                },
                {"requests": duplicates["count"], "tokens": duplicates["input_tokens"]},
            )
        )

    cut_off = [
        index
        for index, exchange in enumerate(exchanges, 1)
        if exchange["stop_reason"] in TRUNCATED
    ]
    for model, indexes in by_model(cut_off).items():
        found.append(
            _finding(
                "cli.watch.output_cut_off",
                {"kind": "model", "id": model},
                window,
                len(indexes),
                references(indexes),
                {"responses": len(indexes), "requests": len(exchanges)},
                {"requests": len(indexes)},
            )
        )

    images = document["images"]
    if images["distinct"]:
        indexes = [
            index
            for index, exchange in enumerate(exchanges, 1)
            if exchange["image_count"]
        ]
        found.append(
            _finding(
                "cli.watch.image_tokens",
                app,
                window,
                images["requests"] or None,
                references(indexes),
                dict(images),
                {"tokens": images["estimated_tokens"]},
            )
        )

    largest = document["largest_tool_result"]
    if largest and largest["bytes"] > LARGE_TOOL_RESULT_BYTES:
        tokens = largest["bytes"] // 4
        found.append(
            _finding(
                "cli.watch.large_tool_result",
                app,
                window,
                largest["requests"] or None,
                [],
                {
                    "bytes": largest["bytes"],
                    "estimated_tokens": tokens,
                    "requests": largest["requests"],
                },
                {"tokens": tokens},
            )
        )
    return found


__all__ = ["RULES", "from_checks", "from_records", "from_watch"]
