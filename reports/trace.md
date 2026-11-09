# Investigation trace: R2.4

Every model and tool call in order. Latency excludes waits; Wait ms is time spent in the client-side token pacer or retry backoff before the request. Tool result tokens are estimates; model tokens are as reported by the provider.


## discovery

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0.734 | 1 | model_call | choice=auto; requested get_release_manifest |  | 459/41/10 | 437 | 0 |
| 2.219 | 1 | tool_call | get_release_manifest {"release":"R2.4"} [ok] | 2522 | ~581 (result) | 1485 |  |
| 2.781 | 2 | model_call | choice=auto; requested list_requirements_for_release |  | 1079/35/6 | 562 | 0 |
| 3.172 | 2 | tool_call | list_requirements_for_release {"release":"R2.4"} [ok] | 4284 | ~963 (result) | 391 |  |
| 4.125 | 3 | model_call | choice=auto; requested submit_requirements |  | 2315/219/13 | 953 | 0 |

## SCRUM-10

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 6.0 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1540/42/8 | 687 | 0 |
| 6.453 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-10"} [ok] | 492 | ~107 (result) | 453 |  |
| 63.031 | 2 | model_call | choice=auto; requested get_issue |  | 1708/65/40 | 762 | 55816 |
| 64.281 | 2 | tool_call | get_issue {"issue_key":"SCRUM-324"} [ok] | 1308 | ~276 (result) | 1250 |  |
| 64.984 | 3 | model_call | choice=auto; requested record_finding |  | 2121/158/94 | 703 | 0 |
| 64.984 | 3 | tool_call | record_finding {"note":"Evidence c11598 in SCRUM-324 shows regression run on release R2.4, commit b7a91f2, service atlas-assistant, environment production with all 146 mandatory cases passing."} [ok] | 15 | ~3 (result) | 0 |  |
| 66.094 | 4 | model_call | choice=auto; requested submit_verdict |  | 2246/175/22 | 731 | 379 |

## SCRUM-14

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 122.891 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1545/40/6 | 575 | 55910 |
| 123.422 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-14"} [ok] | 483 | ~99 (result) | 531 |  |
| 126.328 | 2 | model_call | choice=auto; requested get_issue |  | 1709/32/21 | 1997 | 909 |
| 127.172 | 2 | tool_call | get_issue {"issue_key":"SCRUM-227"} [ok] | 1227 | ~247 (result) | 844 |  |
| 129.297 | 3 | model_call | choice=auto; requested submit_verdict |  | 2092/425/111 | 2125 | 0 |

## SCRUM-13

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 183.406 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1550/40/6 | 1043 | 52722 |
| 184.0 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-13"} [ok] | 534 | ~106 (result) | 594 |  |
| 184.953 | 2 | model_call | choice=auto; requested get_issue |  | 1725/39/28 | 575 | 378 |
| 185.719 | 2 | tool_call | get_issue {"issue_key":"SCRUM-224"} [ok] | 1330 | ~263 (result) | 766 |  |
| 187.844 | 3 | model_call | choice=auto; requested get_issue |  | 2139/78/56 | 622 | 1503 |
| 188.609 | 3 | tool_call | get_issue {"issue_key":"SCRUM-43"} [ok] | 1277 | ~257 (result) | 765 |  |
| 243.344 | 4 | model_call | choice=auto; requested submit_verdict |  | 2550/305/120 | 935 | 53800 |

## SCRUM-7

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 247.891 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1548/40/6 | 622 | 3488 |
| 248.438 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-7"} [ok] | 687 | ~138 (result) | 547 |  |
| 249.125 | 2 | model_call | choice=auto; requested search_issues |  | 1779/83/42 | 687 | 0 |
| 249.563 | 2 | tool_call | search_issues {"kind":"evidence","release":"R2.4"} [ok] | 1233 | ~247 (result) | 438 |  |
| 303.031 | 3 | model_call | choice=auto; requested get_issue |  | 2274/45/23 | 575 | 52893 |
| 303.828 | 3 | tool_call | get_issue {"issue_key":"SCRUM-161"} [ok] | 1257 | ~260 (result) | 797 |  |
| 308.656 | 4 | model_call | choice=auto; requested check_evidence_scope |  | 2675/71/35 | 1325 | 3503 |
| 308.656 | 4 | tool_call | check_evidence_scope {"evidence_keys":["SCRUM-161"],"requirement_key":"SCRUM-7"} [ok] | 775 | ~166 (result) | 0 |  |
| 363.641 | 5 | model_call | choice=auto; requested submit_verdict |  | 2948/205/26 | 1138 | 53847 |

## SCRUM-16

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 367.891 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1550/40/6 | 513 | 3300 |
| 368.406 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-16"} [ok] | 446 | ~94 (result) | 515 |  |
| 369.344 | 2 | model_call | choice=auto; requested get_issue |  | 1717/61/36 | 938 | 0 |
| 370.156 | 2 | tool_call | get_issue {"issue_key":"SCRUM-234"} [ok] | 1563 | ~323 (result) | 812 |  |
| 423.844 | 3 | model_call | choice=auto; requested submit_verdict |  | 2198/400/64 | 1294 | 52394 |

## SCRUM-9

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 427.828 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1544/40/6 | 403 | 3112 |
| 428.375 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-9"} [ok] | 479 | ~97 (result) | 547 |  |
| 429.047 | 2 | model_call | choice=auto; requested get_issue |  | 1707/39/28 | 591 | 81 |
| 429.781 | 2 | tool_call | get_issue {"issue_key":"SCRUM-131"} [ok] | 1238 | ~247 (result) | 734 |  |
| 483.234 | 3 | model_call | choice=auto; requested get_issue |  | 2088/53/31 | 637 | 52816 |
| 484.406 | 3 | tool_call | get_issue {"issue_key":"SCRUM-190"} [ok] | 1360 | ~275 (result) | 1172 |  |
| 489.797 | 4 | model_call | choice=auto; requested submit_verdict |  | 2507/271/70 | 2325 | 3066 |

## SCRUM-15

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 490.797 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1544/42/8 | 563 | 0 |
| 491.359 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-15"} [ok] | 592 | ~119 (result) | 562 |  |
| 543.094 | 2 | model_call | choice=auto; requested get_issue |  | 1749/33/20 | 450 | 51285 |
| 543.719 | 2 | tool_call | get_issue {"issue_key":"SCRUM-401"} [ok] | 1329 | ~266 (result) | 625 |  |
| 548.344 | 3 | model_call | choice=auto; requested check_evidence_scope |  | 2184/137/91 | 825 | 3800 |
| 548.344 | 3 | tool_call | check_evidence_scope {"evidence_keys":["SCRUM-401"],"requirement_key":"SCRUM-15"} [ok] | 1085 | ~221 (result) | 0 |  |
| 551.328 | 4 | model_call | choice=auto; requested submit_verdict |  | 2567/228/30 | 1044 | 1940 |

## SCRUM-6

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 608.281 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1552/40/6 | 715 | 55878 |
| 608.766 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-6"} [ok] | 360 | ~73 (result) | 485 |  |
| 609.125 | 2 | model_call | choice=auto; requested get_issue |  | 1689/30/19 | 359 | 0 |
| 610.375 | 2 | tool_call | get_issue {"issue_key":"SCRUM-349"} [ok] | 1247 | ~250 (result) | 1250 |  |
| 611.063 | 3 | model_call | choice=auto; requested get_issue |  | 2084/36/14 | 688 | 0 |
| 611.734 | 3 | tool_call | get_issue {"issue_key":"SCRUM-89"} [ok] | 1373 | ~279 (result) | 671 |  |
| 672.391 | 4 | model_call | choice=auto; requested check_evidence_scope |  | 2512/112/66 | 2199 | 58458 |
| 672.391 | 4 | tool_call | check_evidence_scope {"evidence_keys":["SCRUM-89"],"requirement_key":"SCRUM-6"} [ok] | 770 | ~160 (result) | 0 |  |
| 728.844 | 5 | model_call | choice=auto; requested submit_verdict |  | 2789/237/14 | 1185 | 55268 |

## SCRUM-11

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 732.313 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1543/40/6 | 482 | 2550 |
| 732.875 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-11"} [ok] | 467 | ~96 (result) | 562 |  |
| 733.219 | 2 | model_call | choice=auto; requested get_issue |  | 1711/43/18 | 344 | 0 |
| 733.922 | 2 | tool_call | get_issue {"issue_key":"SCRUM-117"} [ok] | 1205 | ~244 (result) | 703 |  |
| 788.672 | 3 | model_call | choice=auto; requested get_issue |  | 2100/137/115 | 950 | 53800 |
| 789.438 | 3 | tool_call | get_issue {"issue_key":"SCRUM-184"} [ok] | 1323 | ~271 (result) | 766 |  |
| 793.078 | 4 | model_call | choice=auto; requested submit_verdict |  | 2514/287/78 | 1184 | 2456 |

## SCRUM-8

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 848.031 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1542/42/8 | 262 | 54410 |
| 848.031 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-8"} [ok] | 380 | ~77 (result) | 0 |  |
| 848.594 | 2 | model_call | choice=auto; requested get_issue |  | 1677/33/22 | 563 | 0 |
| 849.953 | 2 | tool_call | get_issue {"issue_key":"SCRUM-26"} [ok] | 1463 | ~293 (result) | 1359 |  |
| 853.281 | 3 | model_call | choice=auto; requested submit_verdict |  | 2140/310/122 | 1340 | 1988 |

## SCRUM-12

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 908.516 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1548/40/6 | 700 | 54160 |
| 909.266 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-12"} [ok] | 489 | ~97 (result) | 750 |  |
| 909.656 | 2 | model_call | choice=auto; requested check_evidence_scope |  | 1714/60/9 | 390 | 0 |
| 911.094 | 2 | tool_call | check_evidence_scope {"evidence_keys":["SCRUM-114","SCRUM-268"],"requirement_key":"SCRUM-12"} [ok] | 1025 | ~202 (result) | 1438 |  |
| 912.891 | 3 | model_call | choice=auto; requested submit_verdict |  | 2050/250/38 | 903 | 894 |

## SCRUM-17

| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning | Latency ms | Wait ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 968.422 | 1 | model_call | choice=auto; requested get_linked_issues |  | 1542/40/6 | 559 | 54597 |
| 968.984 | 1 | tool_call | get_linked_issues {"issue_key":"SCRUM-17"} [ok] | 489 | ~96 (result) | 562 |  |
| 970.219 | 2 | model_call | choice=auto; requested get_issue |  | 1710/31/20 | 903 | 332 |
| 970.922 | 2 | tool_call | get_issue {"issue_key":"SCRUM-326"} [ok] | 1992 | ~390 (result) | 703 |  |
| 973.641 | 3 | model_call | choice=auto; requested get_comments |  | 2306/47/14 | 1607 | 1112 |
| 973.641 | 3 | tool_call | get_comments {"issue_key":"SCRUM-326","limit":5,"query":"PASS"} [ok] | 514 | ~103 (result) | 0 |  |
| 1029.516 | 4 | model_call | choice=auto; requested submit_verdict |  | 2494/224/51 | 1607 | 54268 |
