import json
from datetime import datetime, timedelta
import httplib2
from StringIO import StringIO

from django.template import RequestContext
from django.template.loader import render_to_string
from django.shortcuts import render_to_response, redirect
from django.http import Http404

# noinspection PyUnresolvedReferences
from django.http import HttpResponse, HttpResponseRedirect
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_exempt
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required

# noinspection PyUnresolvedReferences
from django.contrib.auth.models import User
from django.contrib.auth.models import Group as DjangoGroup

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from reader.views import s2_sheets, s2_sheets_by_tag, s2_group_sheets

# noinspection PyUnresolvedReferences
from sefaria.client.util import jsonResponse, HttpResponse
from sefaria.model import *
from sefaria.sheets import *
from sefaria.model.user_profile import *
from sefaria.model.group import Group, GroupSet
from sefaria.system.exceptions import InputError
from sefaria.utils.util import strip_tags

# sefaria.model.dependencies makes sure that model listeners are loaded.
# noinspection PyUnresolvedReferences
import sefaria.model.dependencies

from sefaria.gauth.decorators import gauth_required


def annotate_user_links(sources):
	"""
	Search a sheet for any addedBy fields (containg a UID) and add corresponding user links.
	"""
	for source in sources:
		if "addedBy" in source:
			source["userLink"] = user_link(source["addedBy"])
		if "subsources" in source:
			source["subsources"] = annotate_user_links(source["subsources"])

	return sources

@login_required
@ensure_csrf_cookie
def new_sheet(request):
	"""
	View an new, empty source sheet.
	"""
	if "assignment" in request.GET:
		sheet_id  = int(request.GET["assignment"])

		query = { "owner": request.user.id or -1, "assignment_id": sheet_id }
		existingAssignment = db.sheets.find_one(query) or []
		if "id" in existingAssignment:
			return view_sheet(request,existingAssignment["id"])

		if "assignable" in db.sheets.find_one({"id": sheet_id})["options"]:
			if db.sheets.find_one({"id": sheet_id})["options"]["assignable"] == 1:
				return assigned_sheet(request, sheet_id)

	owner_groups  = get_user_groups(request.user)
	query         = {"owner": request.user.id or -1 }
	hide_video    = db.sheets.find(query).count() > 2


	return render_to_response('sheets.html' if request.COOKIES.get('s1') else 's2_sheets.html', {"can_edit": True,
												"new_sheet": True,
												"is_owner": True,
												"hide_video": hide_video,
												"owner_groups": owner_groups,
												"current_url": request.get_full_path,
												},
												RequestContext(request))


def can_edit(user, sheet):
	"""
	Returns true if user can edit sheet.
	"""
	if sheet["owner"] == user.id:
		return True
	if "collaboration" not in sheet["options"]:
		return False
	if sheet["options"]["collaboration"] == "anyone-can-edit":
		return True
	if sheet["options"]["collaboration"] == "group-can-edit":
		if "group" in sheet:
			if sheet["group"] in [group.name for group in user.groups.all()]:
				return True

	return False


def can_add(user, sheet):
	"""
	Returns true if user has adding persmission on sheet.
	Returns false if user has the higher permission "can_edit"
	"""
	if not user.is_authenticated():
		return False
	if can_edit(user, sheet):
		return False
	if "assigner_id" in sheet:
		if sheet["assigner_id"] == user.id:
			return True
	if "collaboration" not in sheet["options"]:
		return False
	if sheet["options"]["collaboration"] == "anyone-can-add":
		return True
	if sheet["options"]["collaboration"] == "group-can-add":
		if "group" in sheet:
			if sheet["group"] in [group.name for group in user.groups.all()]:
				return True

	return False


def get_user_groups(user):
	"""
	Returns a list of Groups that user belongs to.
	"""
	groups = [g.name for g in user.groups.all()]
	return GroupSet({"name": {"$in": groups }}, sort=[["name", 1]])


def make_sheet_class_string(sheet):
	"""
	Returns a string of class names corresponding to the options of sheet.
	"""
	o = sheet["options"]
	classes = []
	classes.append(o.get("language", "bilingual"))
	classes.append(o.get("layout", "sideBySide"))
	classes.append(o.get("langLayout", "heRight"))

	if o.get("numbered", False):  classes.append("numbered")
	if o.get("boxed", False):     classes.append("boxed")

	if sheet["status"] == "public":
		classes.append("public")


	return " ".join(classes)


@ensure_csrf_cookie
def view_sheet(request, sheet_id):
	"""
	View the sheet with sheet_id.
	"""
	sheet = get_sheet(sheet_id)
	if "error" in sheet:
		return HttpResponse(sheet["error"])

	sheet["sources"] = annotate_user_links(sheet["sources"])

	# Count this as a view
	db.sheets.update({"id": int(sheet_id)}, {"$inc": {"views": 1}})

	try:
		owner = User.objects.get(id=sheet["owner"])
		author = owner.first_name + " " + owner.last_name
		owner_groups = get_user_groups(request.user) if sheet["owner"] == request.user.id else None
	except User.DoesNotExist:
		author = "Someone Mysterious"
		owner_groups = None

	sheet_class     = make_sheet_class_string(sheet)
	can_edit_flag   = can_edit(request.user, sheet)
	can_add_flag    = can_add(request.user, sheet)
	sheet_group     = Group().load({"name": sheet["group"]}) if "group" in sheet and sheet["group"] != "None" else None
	embed_flag      = "embed" in request.GET
	likes           = sheet.get("likes", [])
	like_count      = len(likes)
	viewer_is_liker = request.user.id in likes


	return render_to_response('sheets.html' if request.COOKIES.get('s1') else 's2_sheets.html', {"sheetJSON": json.dumps(sheet),
												"sheet": sheet,
												"sheet_class": sheet_class,
												"can_edit": can_edit_flag,
												"can_add": can_add_flag,
												"title": sheet["title"],
												"author": author,
												"is_owner": request.user.id == sheet["owner"],
												"is_public": sheet["status"] == "public",
												"owner_groups": owner_groups,
												"sheet_group":  sheet_group,
												"like_count": like_count,
												"viewer_is_liker": viewer_is_liker,
												"current_url": request.get_full_path,
											  	"assignments_from_sheet":assignments_from_sheet(sheet_id),
											}, RequestContext(request))

def assignments_from_sheet(sheet_id):
	try:
		query = {"assignment_id": int(sheet_id)}
		return db.sheets.find(query)
	except:
		return None


def view_visual_sheet(request, sheet_id):
	"""
	View the sheet with sheet_id.
	"""
	sheet = get_sheet(sheet_id)
	if "error" in sheet:
		return HttpResponse(sheet["error"])

	sheet["sources"] = annotate_user_links(sheet["sources"])

	# Count this as a view
	db.sheets.update({"id": int(sheet_id)}, {"$inc": {"views": 1}})

	try:
		owner = User.objects.get(id=sheet["owner"])
		author = owner.first_name + " " + owner.last_name
		owner_groups = get_user_groups(request.user) if sheet["owner"] == request.user.id else None
	except User.DoesNotExist:
		author = "Someone Mysterious"
		owner_groups = None

	sheet_class     = make_sheet_class_string(sheet)
	can_edit_flag   = can_edit(request.user, sheet)
	can_add_flag    = can_add(request.user, sheet)
	sheet_group     = Group().load({"name": sheet["group"]}) if "group" in sheet and sheet["group"] != "None" else None
	embed_flag      = "embed" in request.GET
	likes           = sheet.get("likes", [])
	like_count      = len(likes)
	viewer_is_liker = request.user.id in likes


	return render_to_response('sheets_visual.html',{"sheetJSON": json.dumps(sheet),
													"sheet": sheet,
													"sheet_class": sheet_class,
													"can_edit": can_edit_flag,
													"can_add": can_add_flag,
													"title": sheet["title"],
													"author": author,
													"is_owner": request.user.id == sheet["owner"],
													"is_public": sheet["status"] == "public",
													"owner_groups": owner_groups,
													"sheet_group":  sheet_group,
													"like_count": like_count,
													"viewer_is_liker": viewer_is_liker,
													"current_url": request.get_full_path,
											}, RequestContext(request))


@ensure_csrf_cookie
def assigned_sheet(request, assignment_id):
	"""
	A new sheet prefilled with an assignment.
	"""
	sheet = get_sheet(assignment_id)
	if "error" in sheet:
		return HttpResponse(sheet["error"])

	sheet["sources"] = annotate_user_links(sheet["sources"])

	# Remove keys from we don't want transferred
	for key in ("id", "like", "views"):
		if key in sheet:
			del sheet[key]

	assigner        = UserProfile(id=sheet["owner"])
	assigner_id	    = assigner.id
	owner_groups    = get_user_groups(request.user)

	sheet_class     = make_sheet_class_string(sheet)
	can_edit_flag   = True
	can_add_flag    = can_add(request.user, sheet)
	sheet_group     = Group().load({"name": sheet["group"]}) if "group" in sheet and sheet["group"] != "None" else None
	embed_flag      = "embed" in request.GET
	likes           = sheet.get("likes", [])
	like_count      = len(likes)
	viewer_is_liker = request.user.id in likes

	return render_to_response('sheets.html' if request.COOKIES.get('s1') else 's2_sheets.html', {"sheetJSON": json.dumps(sheet),
												"sheet": sheet,
												"assignment_id": assignment_id,
												"assigner_id": assigner_id,
												"new_sheet": True,
												"sheet_class": sheet_class,
												"can_edit": can_edit_flag,
												"can_add": can_add_flag,
												"title": sheet["title"],
												"is_owner": True,
												"is_public": sheet["status"] == "public",
												"owner_groups": owner_groups,
												"sheet_group":  sheet_group,
												"like_count": like_count,
												"viewer_is_liker": viewer_is_liker,
												"current_url": request.get_full_path,
											}, RequestContext(request))

def delete_sheet_api(request, sheet_id):
	"""
	Deletes sheet with id, only if the requester is the sheet owner.
	"""
	import sefaria.search as search
	id = int(sheet_id)
	sheet = db.sheets.find_one({"id": id})
	if not sheet:
		return jsonResponse({"error": "Sheet %d not found." % id})

	if request.user.id != sheet["owner"]:
		return jsonResponse({"error": "Only the sheet owner may delete a sheet."})

	db.sheets.remove({"id": id})
	index_name = search.get_new_and_current_index_names()['current']
	search.delete_sheet(index_name, id)

	return jsonResponse({"status": "ok"})



def sheets_list(request, type=None):
	"""
	List of all public/your/all sheets
	either as a full page or as an HTML fragment
	"""
	if not type:
		# Sheet Splash page

		if request.flavour == "mobile":
			return s2_sheets(request)

		elif not request.COOKIES.get('s1'):
			return s2_sheets(request)

		query       = {"status": "public"}
		public      = db.sheets.find(query).sort([["dateModified", -1]]).limit(32)
		public_tags = recent_public_tags()

		if request.user.is_authenticated():
			query       = {"owner": request.user.id}
			your        = db.sheets.find(query).sort([["dateModified", -1]]).limit(3)
			your_tags   = sheet_tag_counts(query)
			your_tags   = order_tags_for_user(your_tags, request.user.id)
			collapse    = your.count() > 3
		else:
			your = your_tags = collapse = None

		return render_to_response('sheets_splash.html',
									{
										"public_sheets": public,
										"public_tags": public_tags,
										"your_sheets": your,
										"your_tags":   your_tags,
										"collapse_private": collapse,
										"groups": get_user_groups(request.user)
									},
									RequestContext(request))

	response = { "status": 0 }

	if type == "public":
		if request.flavour == "mobile":
			return s2_sheets_by_tag(request,"All Sheets")

		elif not request.COOKIES.get('s1'):
			return s2_sheets_by_tag(request,"All Sheets")

		query              = {"status": "public"}
		response["title"]  = "Public Source Sheets"
		response["public"] = True
		tags               = recent_public_tags()

	elif type == "private":
		if request.flavour == "mobile":
			return s2_sheets_by_tag(request,"My Sheets")

		elif not request.COOKIES.get('s1'):
			return s2_sheets_by_tag(request,"My Sheets")


		query              = {"owner": request.user.id or -1 }
		response["title"]  = "Your Source Sheets"
		response["groups"] = get_user_groups(request.user)
		tags               = sheet_tag_counts(query)
		tags               = order_tags_for_user(tags, request.user.id)

	elif type == "allz":
		query              = {}
		response["title"]  = "All Source Sheets"
		response["public"] = True
		tags               = []

	sheets = db.sheets.find(query).sort([["dateModified", -1]])
	if "fragment" in request.GET:
		return render_to_response('elements/sheet_table.html', {"sheets": sheets})

	response["sheets"] = sheets
	response["tags"]   = tags

	return render_to_response('sheets_list.html', response, RequestContext(request))


def partner_page(request, partner):
	"""
	Views the partner page for 'partner' which lists sheets in the partner group.
	"""
	partner = partner.replace("-", " ").replace("_", " ")
	group   = Group().load({"name": partner})
	if not group:
		raise Http404

	if request.user.is_authenticated() and group.name in [g.name for g in request.user.groups.all()]:
		if not request.COOKIES.get('s1'):
			return s2_group_sheets(request, group.name, True)
		in_group = True
		query = {"status": {"$in": ["unlisted","public"]}, "group": group.name}
	else:
		if not request.COOKIES.get('s1'):
			return s2_group_sheets(request, group.name, False)
		in_group = False
		query = {"status": "public", "group": group.name}


	sheets = db.sheets.find(query).sort([["title", 1]])
	tags   = sheet_tag_counts(query)
	return render_to_response('sheets_list.html', {"sheets": sheets,
												"tags": tags,
												"status": "unlisted",
												"group": group,
												"in_group": in_group,
												"title": "%s on Sefaria" % group.name,
											}, RequestContext(request))


def groups_page(request):
	groups = GroupSet(sort=[["name", 1]])
	return render_to_response("groups.html",
								{"groups": groups},
								RequestContext(request))


@staff_member_required
def groups_api(request):
	j = request.POST.get("json")
	if not j:
		return jsonResponse({"error": "No JSON given in post data."})
	group = json.loads(j)
	if request.method == "POST":
		existing = GroupSet({"name": group["name"]})
		if len(existing):
			existing.update(group)
			existing.save()
		else:
			Group(group).save()
			DjangoGroup.objects.create(name=group["name"])
		return jsonResponse({"status": "ok"})

	elif request.method == "DELETE":
		GroupSet(group).delete()
		return jsonResponse({"status": "ok"})

	else:
		return jsonResponse({"error": "Unsupported HTTP method."})


def sheet_stats(request):
	pass


def sheets_tags_list(request):
	"""
	View public sheets organized by tags.
	"""
	if request.flavour == "mobile":
		return s2_sheets(request)

	elif not request.COOKIES.get('s1'):
		return s2_sheets(request)

	tags_list = make_tag_list()
	return render_to_response('sheet_tags.html', {"tags_list": tags_list, }, RequestContext(request))


def sheets_tag(request, tag, public=True, group=None):
	"""
	View sheets for a particular tag.
	"""
	if public:
		if request.flavour == "mobile":
			return s2_sheets_by_tag(request, tag)
		elif not request.COOKIES.get('s1'):
			return s2_sheets_by_tag(request, tag)
		sheets = get_sheets_by_tag(tag)
	elif group:
		sheets = get_sheets_by_tag(tag, group=group)
	else:
		sheets = get_sheets_by_tag(tag, uid=request.user.id)

	in_group = request.user.is_authenticated() and group in [g.name for g in request.user.groups.all()]
	groupCover = Group().load({"name": group}).coverUrl if Group().load({"name": group}) else None

	return render_to_response('tag.html', {
											"tag": tag,
											"sheets": sheets,
											"public": public,
											"group": group,
											"groupCover": groupCover,
											"in_group": in_group,
										 }, RequestContext(request))

	return render_to_response('sheet_tags.html', {"tags_list": tags_list, }, RequestContext(request))


@login_required
def private_sheets_tag(request, tag):
	"""
	Wrapper for sheet_tag for user tags
	"""
	return sheets_tag(request, tag, public=False)


def partner_sheets_tag(request, partner, tag):
	"""
	Wrapper for sheet_tag for partner tags
	"""
	group = partner.replace("_", " ")
	return sheets_tag(request, tag, public=False, group=group)

@csrf_exempt
def sheet_list_api(request):
	"""
	API for listing available sheets
	"""
	if request.method == "GET":
		return jsonResponse(sheet_list(), callback=request.GET.get("callback", None))

	# Save a sheet
	if request.method == "POST":
		if not request.user.is_authenticated():
			key = request.POST.get("apikey")
			if not key:
				return jsonResponse({"error": "You must be logged in or use an API key to save."})
			apikey = db.apikeys.find_one({"key": key})
			if not apikey:
				return jsonResponse({"error": "Unrecognized API key."})
		else:
			apikey = None

		j = request.POST.get("json")
		if not j:
			return jsonResponse({"error": "No JSON given in post data."})
		sheet = json.loads(j)

		if apikey:
			if "id" in sheet:
				sheet["lastModified"] = get_sheet(sheet["id"])["dateModified"] # Usually lastModified gets set on the frontend, so we need to set it here to match with the previous dateModified so that the check in `save_sheet` returns properly
			user = User.objects.get(id=apikey["uid"])
		else:
			user = request.user

		if "id" in sheet:
			existing = get_sheet(sheet["id"])
			if "error" not in existing  and \
				not can_edit(user, existing) and \
				not can_add(request.user, existing):

				return jsonResponse({"error": "You don't have permission to edit this sheet."})

		if "group" in sheet:
			if sheet["group"] not in [group.name for group in user.groups.all()]:
				sheet["group"] = None

		responseSheet = save_sheet(sheet, user.id)
		if "rebuild" in responseSheet and responseSheet["rebuild"]:
			# Don't bother adding user links if this data won't be used to rebuild the sheet
			responseSheet["sources"] = annotate_user_links(responseSheet["sources"])

		return jsonResponse(responseSheet)


def user_sheet_list_api(request, user_id):
	"""
	API for listing the sheets that belong to user_id.
	"""
	if int(user_id) != request.user.id:
		return jsonResponse({"error": "You are not authorized to view that."})
	return jsonResponse(sheet_list(user_id), callback=request.GET.get("callback", None))

def user_sheet_list_api_with_sort(request, user_id, sort_by="date"):
	if int(user_id) != request.user.id:
		return jsonResponse({"error": "You are not authorized to view that."})
	return jsonResponse(user_sheets(user_id,sort_by), callback=request.GET.get("callback", None))

def private_sheet_list_api(request, partner):
	partner = partner.replace("-", " ").replace("_", " ")
	group   = Group().load({"name": partner})
	if not group:
		raise Http404
	if request.user.is_authenticated() and group.name in [g.name for g in request.user.groups.all()]:
		return jsonResponse(partner_sheets(partner, True), callback=request.GET.get("callback", None))
	else:
		return jsonResponse(partner_sheets(partner, False), callback=request.GET.get("callback", None))


def public_sheet_list_api(request):
	return jsonResponse(sheet_list(), callback=request.GET.get("callback", None))


def sheet_api(request, sheet_id):
	"""
	API for accessing and individual sheet.
	"""
	if request.method == "GET":
		sheet = get_sheet(int(sheet_id))
		return jsonResponse(sheet, callback=request.GET.get("callback", None))

	if request.method == "POST":
		return jsonResponse({"error": "TODO - save to sheet by id"})


def check_sheet_modified_api(request, sheet_id, timestamp):
	"""
	Check if sheet_id has been modified since timestamp.
	If modified, return the new sheet.
	"""
	sheet_id = int(sheet_id)
	callback=request.GET.get("callback", None)
	last_mod = get_last_updated_time(sheet_id)
	if not last_mod:
		return jsonResponse({"error": "Couldn't find last modified time."}, callback)

	if timestamp >= last_mod:
		return jsonResponse({"modified": False}, callback)

	sheet = get_sheet(sheet_id)
	if "error" in sheet:
		return jsonResponse(sheet, callback)

	sheet["modified"] = True
	sheet["sources"] = annotate_user_links(sheet["sources"])
	return jsonResponse(sheet, callback)


def add_source_to_sheet_api(request, sheet_id):
	"""
	API to add a fully formed source (posted as JSON) to sheet_id.

	The contents of the "source" field will be a dictionary.
	The input format is similar to, but differs slightly from, the internal format for sources on source sheets.
	This method reformats the source to the format expected by add_source_to_sheet().

	Fields of input dictionary:
		either `refs` - an array of string refs, indicating a range
			or `ref` - a string ref
			or `outsideText` - a string
			or `outsideBiText` - a dictionary with string fields "he" and "en"
			or `comment` - a string
			or `media` - a URL string

		If the `ref` or `refs` fields are present, the `version`, `he` or `en` fields
		can further specify the origin or content of text for that ref.

	"""
	source = json.loads(request.POST.get("source"))
	if not source:
		return jsonResponse({"error": "No source to copy given."})

	if "refs" in source and source["refs"]:
		ref = Ref(source["refs"][0]).to(Ref(source["refs"][-1]))
		source["ref"] = ref.normal()
		del source["refs"]

	if "ref" in source and source["ref"]:
		ref = Ref(source["ref"])
		source["heRef"] = ref.he_normal()

		if "version" in source or "en" in source or "he" in source:
			text = {}
			if "en" in source:
				text["en"] = source["en"]
				tc = TextChunk(ref, "he", source["version"]) if source.get("versionLanguage") == "he" else TextChunk(ref, "he")
				text["he"] = tc.ja().flatten_to_string()
				del source["en"]
			elif "he" in source:
				text["he"] = source["he"]
				tc = TextChunk(ref, "en", source["version"]) if source.get("versionLanguage") == "en" else TextChunk(ref, "en")
				text["en"] = tc.ja().flatten_to_string()
				del source["he"]
			else:  # "version" in source
				text[source["versionLanguage"]] = TextChunk(ref, source["versionLanguage"], source["version"]).ja().flatten_to_string()
				other = "he" if source["versionLanguage"] == "en" else "en"
				text[other] = TextChunk(ref, other).ja().flatten_to_string()
			source.pop("version", None)
			source.pop("versionLanguage", None)
			source["text"] = text

	return jsonResponse(add_source_to_sheet(int(sheet_id), source))


def copy_source_to_sheet_api(request, sheet_id):
	"""
	API to copy a source from one sheet to another.
	"""
	copy_sheet = request.POST.get("sheet")
	copy_source = request.POST.get("source")
	if not copy_sheet and copy_source:
		return jsonResponse({"error": "Need both a sheet and source number to copy."})
	return jsonResponse(copy_source_to_sheet(int(sheet_id), int(copy_sheet), int(copy_source)))


def add_ref_to_sheet_api(request, sheet_id):
	"""
	API to add a source to a sheet using only a ref.
	"""
	ref = request.POST.get("ref")
	if not ref:
		return jsonResponse({"error": "No ref given in post data."})
	return jsonResponse(add_ref_to_sheet(int(sheet_id), ref))

@login_required
def update_sheet_tags_api(request, sheet_id):
	"""
	API to update tags for sheet_id.
	"""
	tags = json.loads(request.POST.get("tags"))
	return jsonResponse(update_sheet_tags(int(sheet_id), tags))

def visual_sheet_api(request, sheet_id):
	"""
	API for visual source sheet layout
	"""
	if not request.user.is_authenticated():
		return {"error": "You must be logged in to save a sheet layout."}
	if request.method != "POST":
		return jsonResponse({"error": "Unsupported HTTP method."})

	visualNodes = json.loads(request.POST.get("visualNodes"))
	zoomLevel =  json.loads(request.POST.get("zoom"))
	add_visual_data(int(sheet_id), visualNodes, zoomLevel)
	return jsonResponse({"status": "ok"})


def like_sheet_api(request, sheet_id):
	"""
	API to like sheet_id.
	"""
	if not request.user.is_authenticated():
		return {"error": "You must be logged in to like sheets."}
	if request.method != "POST":
		return jsonResponse({"error": "Unsupported HTTP method."})

	add_like_to_sheet(int(sheet_id), request.user.id)
	return jsonResponse({"status": "ok"})


def unlike_sheet_api(request, sheet_id):
	"""
	API to unlike sheet_id.
	"""
	if not request.user.is_authenticated():
		return jsonResponse({"error": "You must be logged in to like sheets."})
	if request.method != "POST":
		return jsonResponse({"error": "Unsupported HTTP method."})

	remove_like_from_sheet(int(sheet_id), request.user.id)
	return jsonResponse({"status": "ok"})


def sheet_likers_api(request, sheet_id):
	"""
	API to retrieve the list of people who like sheet_id.
	"""
	response = {"likers": likers_list_for_sheet(sheet_id)}
	return jsonResponse(response, callback=request.GET.get("callback", None))


def tag_list_api(request, sort_by="count"):
	"""
	API to retrieve the list of public tags ordered by count.
	"""
	response = sheet_tag_counts({"status": "public"}, sort_by)
	response = jsonResponse(response, callback=request.GET.get("callback", None))
	response["Cache-Control"] = "max-age=3600"
	return response


def user_tag_list_api(request, user_id):
	"""
	API to retrieve the list of public tags ordered by count.
	"""
	#if int(user_id) != request.user.id:
		#return jsonResponse({"error": "You are not authorized to view that."})
	response = sheet_tag_counts({ "owner": int(user_id) })
	response = jsonResponse(response, callback=request.GET.get("callback", None))
	response["Cache-Control"] = "max-age=3600"
	return response

def group_tag_list_api(request, partner):
	"""
	API to retrieve the list of public tags ordered by count.
	"""
	partner = partner.replace("-", " ").replace("_", " ")
	response = sheet_tag_counts({ "group": partner })
	response = jsonResponse(response, callback=request.GET.get("callback", None))
	response["Cache-Control"] = "max-age=3600"
	return response


def trending_tags_api(request):
	"""
	API to retrieve the list of peopke who like sheet_id.
	"""
	response = recent_public_tags(days=14)
	response = jsonResponse(response, callback=request.GET.get("callback", None))
	response["Cache-Control"] = "max-age=3600"
	return response


def all_sheets_api(request, limiter, offset=0):
	limiter = int(limiter)
	offset = int(offset)
	query = {"status": "public"}
	if limiter==0:
		public = db.sheets.find(query).sort([["dateModified", -1]])
	else:
		public = db.sheets.find(query).sort([["dateModified", -1]]).skip(offset).limit(limiter)

	sheets   = [sheet_to_dict(s) for s in public]
	response = {"sheets": sheets}
	response = jsonResponse(response, callback=request.GET.get("callback", None))
	response["Cache-Control"] = "max-age=3600"
	return response


def sheets_by_tag_api(request, tag):
	"""
	API to get a list of sheets by `tag`.
	"""
	sheets   = get_sheets_by_tag(tag, public=True)
	sheets   = [sheet_to_dict(s) for s in sheets]
	response = {"tag": tag, "sheets": sheets}
	response = jsonResponse(response, callback=request.GET.get("callback", None))
	response["Cache-Control"] = "max-age=3600"
	return response


def get_aliyot_by_parasha_api(request, parasha):
	response = {"ref":[]};

	if parasha == "V'Zot HaBerachah":
		return jsonResponse({"ref":["Deuteronomy 33:1-7","Deuteronomy 33:8-12","Deuteronomy 33:13-17","Deuteronomy 33:18-21","Deuteronomy 33:22-26","Deuteronomy 33:27-29","Deuteronomy 34:1-12"]}, callback=request.GET.get("callback", None))

	else:
		p = db.parshiot.find({"parasha": parasha}, limit=1).sort([("date", 1)])
		p = p.next()

		for aliyah in p["aliyot"]:
			response["ref"].append(aliyah)
		return jsonResponse(response, callback=request.GET.get("callback", None))



@login_required
def make_sheet_from_text_api(request, ref, sources=None):
	"""
	API to generate a sheet from a ref with optional sources.
	"""
	sources = sources.replace("_", " ").split("+") if sources else None
	sheet = make_sheet_from_text(ref, sources=sources, uid=request.user.id, generatedBy=None, title=None)
	return redirect("/sheets/%d" % sheet["id"])


def sheet_to_html_string(sheet):
	"""
	Create the html string of sheet with sheet_id.
	"""
	sheet["sources"] = annotate_user_links(sheet["sources"])
	sheet = resolve_options_of_sources(sheet)

	try:
		owner = User.objects.get(id=sheet["owner"])
		author = owner.first_name + " " + owner.last_name
	except User.DoesNotExist:
		author = "Someone Mysterious"

	sheet_group = (Group().load({"name": sheet["group"]})
				   if "group" in sheet and sheet["group"] != "None" else None)

	context = {
		"sheetJSON": json.dumps(sheet),
		"sheet": sheet,
		"sheet_class": make_sheet_class_string(sheet),
		"can_edit": False,
		"can_add": False,
		"title": sheet["title"],
		"author": author,
		"is_owner": False,
		"is_public": sheet["status"] == "public",
		"owner_groups": None,
		"sheet_group":  sheet_group,
		"like_count": len(sheet.get("likes", [])),
		"viewer_is_liker": False,
		"assignments_from_sheet": assignments_from_sheet(sheet['id']),
	}

	return render_to_string('gdocs_sheet.html', context).encode('utf-8')


def resolve_options_of_sources(sheet):
	for source in sheet['sources']:
		if 'text' not in source:
			continue
		options = source.setdefault('options', {})
		if not options.get('sourceLanguage'):
			source['options']['sourceLanguage'] = sheet['options'].get(
				'language', 'bilingual')
		if not options.get('sourceLayout'):
			source['options']['sourceLayout'] = sheet['options'].get(
				'layout', 'sideBySide')
		if not options.get('sourceLangLayout'):
			source['options']['sourceLangLayout'] = sheet['options'].get(
				'langLayout', 'heRight')
	return sheet


@gauth_required(scope='https://www.googleapis.com/auth/drive.file', ajax=True)
def export_to_drive(request, credential, sheet_id):
	"""
	Export a sheet to Google Drive.
	"""

	http = credential.authorize(httplib2.Http())
	service = build('drive', 'v3', http=http)

	sheet = get_sheet(sheet_id)
	if 'error' in sheet:
		return jsonResponse({'error': {'message': sheet["error"]}})

	file_metadata = {
		'name': strip_tags(sheet['title'].strip()),
		'mimeType': 'application/vnd.google-apps.document'
	}

	html_string = sheet_to_html_string(sheet)

	media = MediaIoBaseUpload(
		StringIO(html_string),
		mimetype='text/html',
		resumable=True)

	new_file = service.files().create(body=file_metadata,
									  media_body=media,
									  fields='webViewLink').execute()

	return jsonResponse(new_file)
