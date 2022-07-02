from django.shortcuts import render, redirect
from django.http import HttpResponse, HttpResponseForbidden,Http404,JsonResponse
from django.views import View
from django.views.generic.edit import CreateView, FormView, UpdateView, DeleteView
from django.views.generic import ListView
from django.views.generic.base import ContextMixin
from django.views import generic
from django.db.models import Q
from django.db import transaction
from django.db.models.signals import post_save,pre_save
from django.contrib.auth.views import PasswordChangeView, PasswordResetView, PasswordResetDoneView, PasswordResetConfirmView, PasswordResetCompleteView
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm, SetPasswordForm
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.dispatch import receiver
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect,csrf_exempt
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse, reverse_lazy
from django.core.exceptions import *
from .exceptions import FollowerError
from django.utils.text import slugify
from django.contrib import messages
from django.views.decorators.clickjacking import xframe_options_exempt
from .cookies import set_cookie,b64_decode
from .models import User, AnonUser, Post, Article, Like, Comment, ChildComment, CommentArticle, ChildCommentArticle, ChatMessage, ChatRoom, Follower, Notification, Client, Appointment, SavedMessages
from .forms import UserCreationForm, UserLoginForm, UserPasswordChangeForm, UserSetPasswordForm, ChangeUserForm,CreatePostForm, PostCommentForm, CreateArticleForm, AppointmentForm
import json
import threading
import base64
from . import notification
import mimetypes
import datetime
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def get_cookie(request,response,ip):
    anon_user_data = request.COOKIES.get("data",None)
    anon_user = AnonUser.create_user(ip=ip)

    print("Get cookie is working now:",anon_user,anon_user_data,response)

    if anon_user_data is None:
        obj = AnonUser.objects.filter(id__exact=anon_user.id).values()
        value = str(obj[0])
        set_cookie(response,key="data",value=value,days_expire=30)
        return
    return None

def get_client_ip(request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')

        if x_forwarded_for:
            ip = x_forwarded_for
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip

class Main(View):
    def get(self,request, *arg, **kwargs):
        users = User.objects.all()
        posts = Post.objects.all().order_by("-pub_date")
        comments = Comment.objects.all()
        likes = Like.objects.all()

        recent_activity = request.session.get("recent",None)

        context = {
            "users": users,
            "posts": posts,
            "likes": likes,
            "comments": comments,
            "recent_activity": recent_activity,
        }

        if request.user.is_authenticated or request.user.is_anon:
            if request.user.is_authenticated:
                notifications = Notification.objects.filter(to_user__exact=request.user).order_by("-date")
            else:
                notifications = Notification.objects.all()
            post_form = CreatePostForm(request.user)
            comment_form = PostCommentForm
            context["notifications"] = notifications
            context["comment_form"] = comment_form
            context["create_post_form"] = post_form

        response = render(request, "base.html", context)
        return response

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        users = User.objects.all()
        post = Post.objects.all()
        comments = Comment.objects.all()
        likes = Like.objects.all()
        notifications = Notification.objects.filter(to_user__exact=request.user).order_by("-date")
        post_form = CreatePostForm(request.user, request.POST, request.FILES)
        comment_form = PostCommentForm(request.POST)
        try:
            if request.POST["text"] or request.FILES:
                if post_form.is_valid():
                    post_form.save()
            else:
                messages.info(request, "Please write or upload something...")
        except KeyError as e:
            print("Error: ", e)
        context = {
            "users": users,
            "notifications":notifications,
            "posts": post,
            "create_post_form": post_form,
            "comments": comments,
            "likes": likes,
            "comment_form": comment_form
        }
        return render(request, "base.html", context)

class EditPostView(UpdateView):
    model = Post
    fields = ["text","photo"]
    template_name = "pages/edit_post.html"
    context_object_name = "form"
    pk_url_kwarg = "post"
    success_url = reverse_lazy("base:main")

    def get_object(self):
        obj = super().get_object()
        if self.request.user == obj.owner:
            return super().get_object()
        raise PermissionDenied("Access denied")

    def get_queryset(self): 
        return super().get_queryset()

class DeletePostView(DeleteView):
    model = Post
    template_name = "pages/posts/post_confirm_delete.html"
    pk_url_kwarg = "post"
    success_url = reverse_lazy("main")

    def get_object(self):
        obj = super().get_object()
        if self.request.user == obj.owner:
            return super().get_object()
        raise PermissionDenied("Access denied")

    def get_context_data(self,**kwargs):
        context = super().get_context_data(**kwargs)
        back = self.request.META["HTTP_REFERER"]
        context["back"] = back
        return context

class ExplorePostView(generic.DetailView):
    model = Post
    template_name = "pages/explore_post.html"
    context_object_name = "post"
    pk_url_kwarg = "post_id"

class SearchView(ListView):
    model = User
    template_name = "pages/search_results.html"
    context_object_name = "search_results"
    paginate_by = 2

    def get_queryset(self):
        x_forwarded_for = self.request.META.get('HTTP_X_FORWARDED_FOR')
        print(x_forwarded_for)
        query = self.request.GET.get("q","page")
        if self.request.user.is_anonymous or self.request.user.is_anon:        
            object_list = User.objects.filter(
                Q(name__contains=query)|Q(email__contains=query)|Q(name__icontains=query)|Q(email__icontains=query)
            )
            return object_list
        elif self.request.user.is_authenticated:
            object_list = User.objects.filter(
                Q(name__contains=query)|Q(email__contains=query)|Q(name__icontains=query)|Q(email__icontains=query)
            ).exclude(email=self.request.user.email)      
            return object_list

    def get_context_data(self,**kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q","page")
        context["query"] = query
        return context



class PostCommentView(View):
    @transaction.atomic
    def post(self,request,post_id,*args,**kwargs):
        comment_content = request.POST["comment-text"]
        post = Post.objects.get(post_id=post_id)

        if request.user.is_authenticated:
            comment = Comment.objects.filter(user__exact=request.user,post__exact=post)

            # check user has a commment for this post
            if len(comment) > 0:
                comment = Comment.objects.last()
                comment_created = False
            elif len(comment) == 0:
                comment = Comment.objects.create(user=request.user,post=post)
                comment_created = True
            child_comment = ChildComment.objects.create(content=comment_content)
            
            # checking comment is not created 
            if not comment_created:
                data = comment.comment.latest("date")
                data_dict = {}
                for d in data.child_comment.all():
                    data_dict = dict((("user_id",d.user.id),("post_id",d.post_id)))
                if data_dict["user_id"] == request.user.id and str(data_dict["post_id"]) == post_id:
                    comment.comment.add(child_comment)
                else:
                    comment = Comment.objects.create(user=request.user,post=post)
                    comment.comment.add(child_comment)
                    comment.save()
            elif comment_created:
                comment.comment.add(child_comment)

        elif request.user.is_anon:
            comment = Comment.objects.filter(anon_user__exact=request.user,post__exact=post)

            # check user has a commment for this post
            if len(comment) > 0:
                comment = Comment.objects.last()
                comment_created = False
            elif len(comment) == 0:
                comment = Comment.objects.create(anon_user=request.user,post=post)
                comment_created = True
            child_comment = ChildComment.objects.create(content=comment_content)
            
            # checking comment is not created 
            if not comment_created:
                data = comment.comment.latest("date")
                data_dict = {}
                for d in data.child_comment.all():
                    data_dict = dict((("user_id",d.anon_user.id),("post_id",d.post_id)))
                if data_dict["user_id"] == request.user.id and str(data_dict["post_id"]) == post_id:
                    comment.comment.add(child_comment)
                else:
                    comment = Comment.objects.create(anon_user=request.user,post=post)
                    comment.comment.add(child_comment)
                    comment.save()
            elif comment_created:
                comment.comment.add(child_comment)

        return redirect(reverse_lazy("base:main"))

class PostCommentArticleView(View):
    @transaction.atomic
    def post(self,request,pk,*args,**kwargs):
        path = request.META["HTTP_REFERER"]
        comment_content = request.POST["comment-text"]
        article = Article.objects.get(pk=pk)

        if request.user.is_authenticated:
            comment = CommentArticle.objects.filter(user__exact=request.user,article__exact=article)
            if len(comment) > 0:
                comment = CommentArticle.objects.last()
                comment_created = False
            elif len(comment) == 0:
                comment = CommentArticle.objects.create(user=request.user,article=article)
                comment_created = True
            child_comment = ChildCommentArticle.objects.create(content=comment_content)
            if not comment_created:
                data = comment.comment.latest("date")
                data_dict = {}
                for d in data.child_comment_article.all():
                    data_dict = dict((("user_id",d.user.id),("article_id",d.article_id)))
                if data_dict["user_id"] == request.user.id and int(data_dict["article_id"]) == pk:
                    comment.comment.add(child_comment)
                else:
                    comment = CommentArticle.objects.create(user=request.user,article=article)
                    comment.comment.add(child_comment)
                    comment.save()
            elif comment_created:
                comment.comment.add(child_comment)

        elif request.user.is_anon:
            comment = CommentArticle.objects.filter(anon_user__exact=request.user,article__exact=article)
            if len(comment) > 0:
                comment = CommentArticle.objects.last()
                comment_created = False
            elif len(comment) == 0:
                comment = CommentArticle.objects.create(anon_user=request.user,article=article)
                comment_created = True
            child_comment = ChildCommentArticle.objects.create(content=comment_content)
            if not comment_created:
                data = comment.comment.latest("date")
                data_dict = {}
                for d in data.child_comment_article.all():
                    data_dict = dict((("user_id",d.user.id),("article_id",d.article_id)))
                if data_dict["user_id"] == request.user.id and int(data_dict["article_id"]) == pk:
                    comment.comment.add(child_comment)
                else:
                    comment = CommentArticle.objects.create(anon_user=request.user,article=article)
                    comment.comment.add(child_comment)
                    comment.save()
            elif comment_created:
                comment.comment.add(child_comment)
        return redirect(path)    

# Article section

class ShowArticles(ListView):
    model = Article
    template_name = "pages/articles/show_articles.html"
    context_object_name = "articles"

    def get_queryset(self):
        article = Article.objects.all().order_by("-likes_count")
        return article


class CreateArticleView(LoginRequiredMixin,CreateView):
    form_class = CreateArticleForm
    template_name = "pages/create_article.html"
    success_url = reverse_lazy("main")

    def handle_no_permission(self):
        print(super().handle_no_permission())
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["article_form"] = CreateArticleForm
        return context
    
    def form_valid(self, form):
        form.instance.author = self.request.user
        data = form.save()
        data.comments = data
        data.save()
        url = form.instance.get_absolute_url()
        return redirect(url)
    
    def form_invalid(self, form):
        data = form.errors.as_json()
        data_json = json.loads(data)
        message = data_json["body"][0]["message"]
        messages.error(self.request, message)
        return super().form_invalid(form)  
    
class ArticleDetailView(generic.detail.DetailView):
    model = Article
    template_name = "pages/article_detail.html"
    context_object_name = "article"
    query_pk_and_slug = True

    def get_context_data(self,**kwargs):
        context = super().get_context_data(**kwargs)
        article = Article.objects.get(pk=self.kwargs["pk"])
        print(datetime.datetime.date)
        self.request.session["recent"] = {
            "date": str(datetime.datetime.now())[:10],
            "time": str(datetime.datetime.now())[10:16],
            "headline":article.headline,
            "pk":article.pk
        }
        if not self.request.user.is_anonymous:
            related_articles = Article.objects.filter(Q(headline__icontains=article.headline)|Q(body__icontains=article.headline)|Q(body__icontains=article.headline)|Q(body__icontains=article.body)|Q(headline__in=article.body)|Q(headline__in=article.headline)|Q(headline__istartswith=article.headline)|Q(headline__startswith=article.headline)|Q(author__exact=article.author)).exclude(pk__exact=article.pk)
            context["related_articles"] = related_articles
            return context
        related_articles = Article.objects.filter(Q(headline__icontains=article.headline)|Q(body__icontains=article.headline)|Q(body__icontains=article.headline)|Q(body__icontains=article.body)|Q(headline__in=article.body)|Q(headline__in=article.headline)|Q(headline__istartswith=article.headline)|Q(headline__startswith=article.headline))
        context["related_articles"] = related_articles
        return context

class EditArticleView(LoginRequiredMixin,UpdateView):
    model = Article
    fields = ["headline","body"]
    template_name = "pages/articles/edit_article.html"
    context_object_name = "form"
    pk_url_kwarg = "pk"
    success_url = reverse_lazy("main")

    def get_object(self):
        obj = super().get_object()
        if self.request.user == obj.author:
            return super().get_object()
        raise PermissionDenied("Access denied")

    def get_queryset(self): 
        return super().get_queryset()

class DeleteArticleView(LoginRequiredMixin,DeleteView):
    model = Article
    template_name = "pages/articles/article_confirm_delete.html"
    pk_url_kwarg = "article"
    success_url = reverse_lazy("main")

    def get_object(self):
        obj = super().get_object()
        if self.request.user == obj.author:
            return super().get_object()
        raise PermissionDenied("Access denied")

    def get_context_data(self,**kwargs):
        context = super().get_context_data(**kwargs)
        obj = self.get_object()
        back = self.request.META["HTTP_REFERER"]
        context["article"] = obj.headline
        context["back"] = back
        return context

class LikeArticleView(View):
    def get_client_ip(self):
        x_forwarded_for = self.request.META.get('HTTP_X_FORWARDED_FOR')

        if x_forwarded_for:
            ip = x_forwarded_for
        else:
            ip = self.request.META.get('REMOTE_ADDR')
        return ip

    def get(self,request,pk):
        redirect_url = request.META.get("HTTP_REFERER")

        if redirect_url:
            response = redirect(redirect_url)
            article = Article.objects.get(pk=pk)
            ip = self.get_client_ip()
            get_cookie(request,response,ip)

            try:
                if request.user.is_authenticated:
                    like = article.likes.get(user=request.user,like=True)
                    article.likes.remove(like)
                    like.delete()
                elif request.user.is_anon:
                    like = article.likes.get(anon_user=request.user,like=True)
                    article.likes.remove(like)
                    like.delete()
                else:
                    messages.info(request,"You should login as doctor or client")
            except Like.DoesNotExist:
                if request.user.is_authenticated:
                    like = Like.objects.create(user=request.user,like=True)
                    article.likes.add(like)
                elif request.user.is_anon:
                    like = Like.objects.create(anon_user=request.user,like=True)
                    article.likes.add(like)
            return response
        raise PermissionDenied("Invalid url.")

# like a post
class LikePostView(View):
    def get_client_ip(self):
        x_forwarded_for = self.request.META.get('HTTP_X_FORWARDED_FOR')

        if x_forwarded_for:
            ip = x_forwarded_for
        else:
            ip = self.request.META.get('REMOTE_ADDR')
        return ip

    def get_post(self,post_id):
        try:
            return Post.objects.get(post_id=post_id)
        except Post.DoesNotExist:
            raise Http404("Post not found.")

    def get(self,request,*args,**kwargs):
        redirect_url = request.META.get("HTTP_REFERER")
        if redirect_url:
            response = redirect(redirect_url)
            post_id = kwargs["post_id"]
            ip = self.get_client_ip()
            get_cookie(request,response,ip)
            post = self.get_post(post_id)
            try:
                if request.user.is_authenticated:
                    like = post.likes.get(user=request.user,like=True)
                    post.likes.remove(like)
                    like.delete()

                # Check the user is anonymous and have registered by custom AnonUser model
                elif request.user.is_anon:
                    like = post.likes.get(anon_user=request.user,like=True)
                    post.likes.remove(like)
                    like.delete()
                else:
                    like = post.likes.get(anon_user=request.user,like=True)
                    post.likes.remove(like)
                    like.delete()
            except Like.DoesNotExist:
                if request.user.is_authenticated:
                    like = Like.objects.create(user=request.user,like=True)
                    post.likes.add(like)

                # Check the user is anonymous and have registered by custom AnonUser model
                elif request.user.is_anon:
                    like = Like.objects.create(anon_user=request.user,like=True)
                    post.likes.add(like)
                else:
                    like = Like.objects.create(anon_user=request.user,like=True)
                    post.likes.add(like)

            return response
        raise PermissionDenied("Invalid url.")

# Dashboard
class DashboardView(LoginRequiredMixin, View, ContextMixin):
    login_url = reverse_lazy("login")
    redirect_field_name = reverse_lazy("dashboard")

    def get(self, request, user,**kwargs):
        ctx = self.get_context_data()
        form = ChangeUserForm(initial={'name':request.user.name,'email':request.user.email,"image":request.user.image})
        context = {
            "followers": ctx["followers"],
            "posts": ctx["posts"],
            "posts_count": ctx["posts_count"],
            "articles": ctx["articles"],
            "articles_count": ctx["articles_count"],
            "form":form,
        }
        return render(request, 'pages/dashboard.html', context)

    def post(self, request, *args, **kwargs):
        form = ChangeUserForm(request.POST,request.FILES)
        if form.is_valid():
            name = form.cleaned_data["name"]
            email = form.cleaned_data["email"]
            image = form.cleaned_data["image"]
            try:
                user = User.objects.get(email=request.user.email)
                if name is not None:
                    user.name = name
                    user.save()
                if len(email) > 0:
                    user.email = email
                    user.save()
                if image is not None:
                    user.image = image
                    user.save()
                messages.info(request,"Your changes are successfully changed")
            except Exception as e:
                messages.info(request,"This email is already exists")
                       
        ctx = self.get_context_data()
        context = {
            "followers": ctx["followers"],
            "posts": ctx["posts"],
            "posts_count": ctx["posts_count"],
            "articles": ctx["articles"],
            "articles_count": ctx["articles_count"],
            "form":form,
        }
        return render(request, "pages/dashboard.html",context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current_user = self.request.user
        current_user.refresh_from_db()
        context["followers"] = Follower.get_count(self.request.user)
        context["posts"] = Post.objects.filter(owner__exact=self.request.user)
        context["posts_count"] = Post.get_count(self.request.user)
        context["articles"] = Article.objects.filter(author__exact=self.request.user)
        context["articles_count"] = Article.get_count(author=self.request.user)
        
        return context


class UserProfileShowcaseView(View):
    def get(self, request, username, user_id, *args, **kwargs):
        user = User.objects.get(pk=user_id)
        posts = Post.objects.filter(owner=user).values().order_by("-pub_date")
        articles = Article.objects.filter(author=user)
        followed = False
        if request.user.is_authenticated:
            try:
                follower = Follower.objects.get(user=user)
                if request.user in follower.followers.all():
                    followed = True
            except Follower.DoesNotExist:
                followed = True
        context = {
            "user": user,
            "posts": posts,
            "articles": articles,
            "followed": followed
        }
        return render(request, "pages/user_profile_showcase.html", context)

@csrf_exempt
def chat_api(request):
    room_id = None
    if request.method == "POST":
        data = request.POST.dict()
        data_json = json.loads(data["data"])
        request_user_id = data_json["requestUserId"]
        user_id = data_json["userId"]
        user,user_2 = User.objects.get(pk=request_user_id),User.objects.get(pk=user_id)
        try:
            room_id = ChatRoom.get_room_id(outgoing_user=user,incoming_user=user_2)
        except ChatRoom.DoesNotExist:
            pass
        print("ROOM ID: ",room_id)
        return JsonResponse({"room_id": room_id})
    return JsonResponse({"room_id": room_id})

class ChatRoomView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        context = {}
        message_users = ChatRoom.objects.filter(Q(outgoing=request.user)|Q(incoming=request.user))
        context["message_users"] = message_users
        try:
            if kwargs["user_id"]:
                user = User.objects.get(pk=kwargs["user_id"])
                messages = ChatRoom.objects.filter((Q(outgoing=request.user) & Q(incoming=user)) | (Q(outgoing=user) & Q(incoming=request.user)))
                context["messages"] = messages
                context["user"] = user
        except KeyError:
            print("KeyError occured")
        return render(request, "pages/chat.html", context)
    def post(self, request, *args, **kwargs):
        message = request.POST.get("message_text")
        context = {}
        try:
            if kwargs["user_id"]:
                user = User.objects.get(pk=kwargs["user_id"])
                context["user"] = user
        except KeyError:
            print("KeyError occured")
        return render(request, "pages/chat.html", context)
class Register(CreateView):
    form_class = UserCreationForm
    template_name = "auth/register.html"
    success_url = "/login"

    def form_valid(self, form):
        if form.is_valid():
            form.save(commit=True)
            messages.success(
                self.request, f"Account created for {form.cleaned_data.get('email')}")
        return super().form_valid(form)


class Login(View):
    def get(self, request, *args, **kwargs):
        context = {}
        context["form"] = UserLoginForm
        return render(request, "auth/login.html", context)

    def post(self, request, *args, **kwargs):
        form = UserLoginForm(request.POST)
        if form.is_valid():
            try:
                user = authenticate(
                    request, email=form.cleaned_data["email"], password=form.cleaned_data["password"])
                login(request, user)
                return redirect("base:main")
            except Exception as e:
                print("error: ", e)
                messages.info(request, e.message)
        context = {
            "form": form
        }
        return render(request, "auth/login.html", context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_error"] = self.context
        return context

class FollowView(View):
    def get(self, request, user_id,*args, **kwargs):
        path = request.META.get('HTTP_REFERER')
        user = User.objects.get(pk=user_id)
        try:
            if request.user.is_authenticated:
                follower = Follower.objects.get(user=user)
                followers = follower.followers.all()
                if request.user in followers:
                    Follower.unfollow(user=user, follower=request.user)
                else:
                    raise FollowerError("UserDoesNotExist")
            if request.user.is_anon:
                follower = Follower.objects.get(user=user)
                followers = follower.anon_followers.all()
                if request.user in followers:
                    Follower.unfollow(user=user, follower=request.user)
                else:
                    raise FollowerError("UserDoesNotExist")
        except (FollowerError,Follower.DoesNotExist):
            if request.user.is_authenticated:
                if not user == request.user:
                    Follower.follow(user=user, follower=request.user)
            if request.user.is_anon:
                Follower.follow(user=user, follower=request.user)
        return redirect(path)

# Video Stream
class LiveStreamView(View):
    def get(self,request,*args,**kwargs):
        return render(request,"pages/video_stream.html")

# To make appointment with doctor.
class MakeAppointmentView(View,ContextMixin):
    def get_appointment(self,doctor):
        try:
            return Appointment.objects.get(doctor=doctor)
        except Appointment.DoesNotExist:
            return None

    def get(self,request,*args,**kwargs):
        doctor_id = kwargs.get("doctor_id",None)
        doctor = self.get_doctor(doctor_id)
        appointment = self.get_appointment(doctor)
        button_name = "Make Appointment"
        context = {
            "form": AppointmentForm,
            "button_name": button_name,
        }
        ip = get_client_ip(request)
        response = render(request,"pages/appointment.html")
        get_cookie(request,response,ip)
        if request.user.is_anon:
            if appointment is not None:
                appointments = appointment.clients.filter(client=request.user)
                if len(appointments) > 0:
                    context["button_name"] = "Update Appointment"
                    form = AppointmentForm(initial={"reason": appointments[0].reason})
                    context["form"] = form
        return render(request,"pages/appointment.html",context)

    def get_doctor(self,doctor_id):
        try:
            return User.objects.get(id=doctor_id)
        except User.DoesNotExist:
            raise Http404("Doctor not found.")

    def post(self,request,*args,**kwargs):
        doctor_id = kwargs.get("doctor_id",None)
        form = AppointmentForm(request.POST)
        button_name = "Upadate Button"
        context = {
            "form": form,
            "button_name": button_name,
        }
        if doctor_id is not None:
            doctor = self.get_doctor(doctor_id)
            if form.is_valid(): 
                reason = form.cleaned_data.get("reason")
                if request.user.is_anon:
                    appointment,created = Appointment.objects.get_or_create(doctor=doctor)
                    if created:
                        client = Client.objects.create(client=request.user,reason=reason)
                        appointment.clients.add(client)
                        appointment.save()
                    else:
                        appointments = appointment.clients.filter(client=request.user)
                        appointment_id = appointments[0].id
                        appointment = Client.objects.get(id=appointment_id)
                        appointment.reason = reason
                        appointment.save()
                elif request.user.is_anonymous:
                    messages.info(request,"You should register as doctor or client.")
        return render(request,"pages/appointment.html",context)

# To save saved_messages
class SavedMessagesView(View):
    def get(self,request,message_type,message_id,*args,**kwargs):
        #match message_type:
        if message_type == "post":
            self.create_post_message(request,message_id)
        elif message_type == "article":
                self.create_article_message(request,message_id)

        return redirect("base:main")

    def get_post(self,post_id):
        try:
            return Post.objects.get(post_id=post_id)
        except Post.DoesNotExist:
            raise Http404("Post not found.")

    def create_post_message(self,request,post_id):
        post = self.get_post(post_id)
        if request.user.is_authenticated:
            saved_message,created = SavedMessages.objects.get_or_create(user=request.user)
            if not created:
                saved_message.posts.remove(post)
            else:
                saved_message.save()
                saved_message.posts.add(post)

        elif request.user.is_anon:
            saved_message,created = SavedMessages.objects.get_or_create(anon_user=request.user)
            if not created:
                saved_message.posts.remove(post)
            else:
                saved_message.save()
                saved_message.posts.add(post)

    def get_article(self,article_id):
        try:
            return Article.objects.get(pk=article_id)
        except Article.DoesNotExist:
            raise Http404("Article not found.")

    def create_article_message(Self,request,article_id):
        article = self.get_article(article_id)
        if request.user.is_authenticated:
            saved_message,created = SavedMessages.objects.get_or_create(user=request.user)
            if not created:
                saved_message.articles.remove(article)
            else:
                saved_message.save()
                saved_message.articles.add(article)

        elif request.user.is_anon:
            saved_message,created = SavedMessages.objects.get_or_create(anon_user=request.user)
            if not created:
                saved_message.articles.remove(article)
            else:
                saved_message.save()
                saved_message.articles.add(article)

# Realted to Authentiction
def logout_view(request):
    logout(request)
    return redirect("base:main")


class UserPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    form_class = UserPasswordChangeForm
    template_name = "auth/password_change.html"
    success_url = reverse_lazy("dashboard")

class UserPasswordResetView(PasswordResetView):
    form_class = PasswordResetForm
    template_name = "auth/password_reset.html"
    email_template_name = "auth/password_reset_email.html"
    success_url = reverse_lazy("base:password-reset-done")


class UserPasswordResetDoneView(PasswordResetDoneView):
    template_name = "auth/password_reset_done.html"


class UserPasswordResetConfirmView(PasswordResetConfirmView):
    form_class = UserSetPasswordForm
    template_name = "auth/password_reset_confirm.html"
    success_url = reverse_lazy("base:password-reset-complete")


class UserPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = "auth/password_reset_complete.html"
